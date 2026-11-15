#!/usr/bin/env python3
"""Measure the staging site against the live Shopify site at 1440p.

Visual regression claims ("fonts are smaller", "logo is smaller", "titles get cut
off") are measurable rather than subjective: computed font sizes, element boxes,
text clipping and scrollbar state can all be read from a headless browser. This
script loads both sites, reports the numbers and saves screenshots for the eye.
"""

from __future__ import annotations

import json
import os
import sys

from playwright.sync_api import sync_playwright

OUT = "/tmp/vis"
os.makedirs(OUT, exist_ok=True)

# label -> (shopify url, staging url, page kind)
PAGES = {
	"home": ("https://glia.org/", "https://asset.glia.org/", "home"),
	"product": ("https://glia.org/products/stethoscope", "https://asset.glia.org/stethoscope", "product"),
	"collection": ("https://glia.org/collections/glia", "https://asset.glia.org/all-products", "collection"),
	"article": (
		"https://glia.org/blogs/news/against-all-odds-manufacturing-external-fixators-in-gaza",
		"https://asset.glia.org/blog/news/against-all-odds-manufacturing-external-fixators-in-gaza",
		"article",
	),
	"contact": ("https://glia.org/pages/contact", "https://asset.glia.org/contact", "contact"),
	"press": ("https://glia.org/pages/press", "https://asset.glia.org/press-releases", "press"),
}

MEASURE_JS = r"""
() => {
  const px = (v) => Math.round(parseFloat(v) * 100) / 100;
  const r = (el) => { const b = el.getBoundingClientRect(); return {w: Math.round(b.width), h: Math.round(b.height)}; };
  const cs = (el, props) => { const s = getComputedStyle(el); const o = {}; for (const p of props) o[p] = s[p]; return o; };
  const main = document.querySelector('main') || document.body;

  // paragraphs that actually carry copy
  const paras = [...main.querySelectorAll('p')]
    .filter(p => (p.innerText || '').trim().length > 40 && p.getBoundingClientRect().height > 0)
    .slice(0, 8);
  const paraSizes = paras.map(p => px(getComputedStyle(p).fontSize));
  const paraStyles = paras.length ? cs(paras[0], ['font-size','line-height','font-family']) : null;

  const h1 = main.querySelector('h1');
  const h2 = main.querySelector('h2');

  // header logo
  const logoEl = document.querySelector('header img, .header__heading-logo, .site-header img, nav img, .glia-logo img, .glia-brand img');
  const logo = logoEl ? {...r(logoEl), ...cs(logoEl, ['width','height'])} : null;

  // buttons that matter: add to cart / donate / explore
  const btns = [...document.querySelectorAll('button, a.btn, a.button, .btn, input[type=submit]')]
    .filter(b => /add to cart|donate|explore|buy now|subscribe/i.test(b.innerText || b.value || ''))
    .slice(0, 6)
    .map(b => ({text: (b.innerText || b.value || '').trim().slice(0, 28), ...r(b),
                ...cs(b, ['font-size','background-color','color','border','border-radius','padding'])}));

  // card titles: clipping check
  const titleEls = [...main.querySelectorAll('h2, h3, .card__heading, .product-title, .item-card h3, .list-row h3')]
    .filter(el => (el.innerText || '').trim().length > 6)
    .slice(0, 12)
    .map(el => ({text: (el.innerText || '').trim().slice(0, 40), ...r(el),
                 fontSize: px(getComputedStyle(el).fontSize),
                 clamp: getComputedStyle(el).webkitLineClamp,
                 clipped: el.scrollHeight > el.clientHeight + 1,
                 overflow: getComputedStyle(el).overflow}));

  // scroll containers that show a scrollbar with nothing to scroll
  const scrollables = [...main.querySelectorAll('*')].filter(el => {
    const s = getComputedStyle(el);
    return /(auto|scroll)/.test(s.overflowY) || /(auto|scroll)/.test(s.overflowX);
  }).slice(0, 25).map(el => {
    const s = getComputedStyle(el);
    return {tag: el.tagName.toLowerCase(), cls: (el.className || '').toString().slice(0, 40),
            oy: s.overflowY, ox: s.overflowX,
            vScroll: el.scrollHeight - el.clientHeight, hScroll: el.scrollWidth - el.clientWidth};
  });
  const uselessScrollbars = scrollables.filter(s =>
      (/(scroll)/.test(s.oy) && s.vScroll <= 0) || (/(scroll)/.test(s.ox) && s.hScroll <= 0));

  return {
    path: location.pathname,
    viewport: {w: innerWidth, h: innerHeight},
    docWidth: document.documentElement.scrollWidth,
    horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1,
    body: cs(document.body, ['font-size','font-family']),
    paraStyles, paraSizes, paraMedian: paraSizes.length ? paraSizes.sort((a,b)=>a-b)[Math.floor(paraSizes.length/2)] : null,
    h1: h1 ? {size: px(getComputedStyle(h1).fontSize), ...r(h1)} : null,
    h2: h2 ? {size: px(getComputedStyle(h2).fontSize)} : null,
    logo, buttons: btns, titles: titleEls,
    scrollables, uselessScrollbars,
  };
}
"""


def measure(page, url: str) -> dict:
	page.goto(url, wait_until="domcontentloaded", timeout=60000)
	try:
		page.wait_for_load_state("networkidle", timeout=15000)
	except Exception:
		pass
	page.wait_for_timeout(1200)
	# nudge lazyloaded content
	page.evaluate("window.scrollTo(0, document.body.scrollHeight/3)")
	page.wait_for_timeout(600)
	page.evaluate("window.scrollTo(0, 0)")
	page.wait_for_timeout(400)
	data = page.evaluate(MEASURE_JS)
	data["url"] = url
	return data


def main() -> None:
	labels = sys.argv[1:] or list(PAGES)
	results = {}
	with sync_playwright() as p:
		browser = p.chromium.launch()
		ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
		page = ctx.new_page()
		for label in labels:
			shopify_url, ours_url, kind = PAGES[label]
			entry = {}
			for who, url in (("shopify", shopify_url), ("staging", ours_url)):
				try:
					entry[who] = measure(page, url)
					page.screenshot(path=f"{OUT}/{label}-{who}.png", full_page=False)
					page.screenshot(path=f"{OUT}/{label}-{who}-full.png", full_page=True)
				except Exception as exc:
					entry[who] = {"error": str(exc)}
				print(f"measured {label}/{who}: {'ok' if 'error' not in entry[who] else entry[who]['error']}")
			results[label] = entry
		browser.close()
	path = f"{OUT}/measure.json"
	merged = {}
	if os.path.exists(path):
		merged = json.load(open(path))
	merged.update(results)
	json.dump(merged, open(path, "w"), indent=1)

	print("\n== summary ==")
	for label, e in results.items():
		s, o = e.get("shopify", {}), e.get("staging", {})
		if "error" in s or "error" in o:
			print(f"{label}: measurement error")
			continue
		def g(d, *keys, default="-"):
			for k in keys:
				if not isinstance(d, dict) or k not in d:
					return default
				d = d[k]
			return d
		print(f"\n{label}")
		print(f"  body font   shopify={g(s,'body','font-size')}  staging={g(o,'body','font-size')}")
		print(f"  para median shopify={s.get('paraMedian')}  staging={o.get('paraMedian')}")
		print(f"  h1 size     shopify={g(s,'h1','size')}  staging={g(o,'h1','size')}")
		print(f"  logo w/h    shopify={g(s,'logo','w')}x{g(s,'logo','h')}  staging={g(o,'logo','w')}x{g(o,'logo','h')}")
		print(f"  clipped titles shopify={sum(1 for t in s.get('titles',[]) if t.get('clipped'))}/{len(s.get('titles',[]))}"
		      f"  staging={sum(1 for t in o.get('titles',[]) if t.get('clipped'))}/{len(o.get('titles',[]))}")
		print(f"  useless scrollbars shopify={len(s.get('uselessScrollbars',[]))} staging={len(o.get('uselessScrollbars',[]))}")
		print(f"  horizontal overflow shopify={s.get('horizontalOverflow')} staging={o.get('horizontalOverflow')}")
		if s.get("buttons") and o.get("buttons"):
			bs, bo = s["buttons"][0], o["buttons"][0]
			print(f"  button '{bs.get('text')}' shopify: bg={bs.get('background-color')} border={bs.get('border')[:24]} radius={bs.get('border-radius')} fs={bs.get('font-size')}")
			print(f"  button '{bo.get('text')}' staging: bg={bo.get('background-color')} border={bo.get('border')[:24]} radius={bo.get('border-radius')} fs={bo.get('font-size')}")


if __name__ == "__main__":
	main()

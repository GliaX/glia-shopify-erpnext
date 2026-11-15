#!/usr/bin/env python3
"""Rebuild thin ERPNext pages from the rendered Shopify pages.

Shopify's Admin API returns an *empty* `body` for section-built pages (Meet the
Team, FAQs, Glia X Rajaeen, ...), which is why the migration left them nearly
blank. The rendered storefront page does contain the content, so this script:

1. fetches https://glia.org/pages/<handle>
2. keeps only the semantic content inside <main> (headings, paragraphs, lists,
   images, links, quotes, tables) and drops Shopify chrome, scripts and forms
3. downloads every Shopify CDN image, uploads it to ERPNext /files and rewrites
   the src
4. maps Shopify URLs onto the new site (/pages/x -> /x, /products/x -> /x,
   /collections/* -> /all-products)
5. writes the result into the Web Page's main_section_html

Usage:
    python3 scrape_pages.py [handle ...]        # default: all mapped pages
    python3 scrape_pages.py --dry-run meet-the-team
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request

from bs4 import BeautifulSoup

SHOPIFY = "https://glia.org"
ERPNEXT = os.environ.get("ERPNEXT_BASE_URL", "https://asset.glia.org").rstrip("/")
COOKIE_JAR = os.environ.get("ERPNEXT_COOKIE_JAR", "/tmp/fj.jar")
CACHE = "/tmp/shopify-scrape-cache"
UA = "Mozilla/5.0 (compatible; GliaMigration/1.0)"

# shopify handle -> (Web Page name, Web Page route)
PAGES = {
	"meet-the-team": ("meet-the-team", "meet-the-team"),
	"faqs": ("faqs", "faqs"),
	"glia-x-rajaeen": ("glia-x-rajaeen", "glia-x-rajaeen"),
	"the-glia-x-rajaeen-collection": (
		"the-glia-x-rajaeen-collection",
		"the-glia-x-rajaeen-collection",
	),
	"restoring-care-in-gaza": ("restoring-care-in-gaza", "restoring-care-in-gaza"),
	"glia4gaza": (
		"support-glia-medical-solidarity-with-palestine",
		"glia4gaza",
	),
	"run-for-palestine-2026": ("run-for-palestine-2026", "run-for-palestine-2026"),
}

ALLOWED = {"h1", "h2", "h3", "h4", "h5", "p", "ul", "ol", "li", "blockquote",
           "strong", "b", "em", "i", "br", "a", "img", "figure", "figcaption",
           "table", "thead", "tbody", "tr", "th", "td", "hr"}
DROP = {"script", "style", "noscript", "svg", "iframe", "form", "nav", "header",
        "footer", "button", "input", "select", "textarea", "video", "audio"}

os.makedirs(CACHE, exist_ok=True)


def get(url: str) -> bytes:
	key = hashlib.sha256(url.encode()).hexdigest()[:16]
	path = os.path.join(CACHE, key)
	if os.path.exists(path):
		return open(path, "rb").read()
	req = urllib.request.Request(url, headers={"User-Agent": UA})
	data = urllib.request.urlopen(req, timeout=60).read()
	open(path, "wb").write(data)
	return data


def upload_file(path: str) -> str:
	"""Upload a local file to ERPNext /files and return its file_url."""
	out = subprocess.run(
		["curl", "-s", "-b", COOKIE_JAR, "-X", "POST",
		 f"{ERPNEXT}/api/method/upload_file",
		 "-H", "X-Frappe-CSRF-Token: None",
		 "-F", "is_private=0", "-F", "folder=Home", "-F", f"file=@{path}"],
		capture_output=True, text=True, check=True,
	)
	payload = json.loads(out.stdout)
	return payload["message"]["file_url"]


def cdn_url(raw: str) -> str | None:
	if not raw:
		return None
	raw = raw.split()[0].strip()
	if raw.startswith("//"):
		raw = "https:" + raw
	if raw.startswith("/"):
		raw = SHOPIFY + raw
	if "cdn.shopify.com" not in raw and "glia.org/cdn" not in raw:
		return None
	# glia.org/cdn/shop/files/... is the storefront's own CDN path and resolves
	# correctly; only strip the cache-busting query.
	return raw.split("?")[0]


def localise_image(src: str, cache: dict) -> str:
	if src in cache:
		return cache[src]
	candidate = src
	try:
		data = get(candidate)
	except Exception as exc:  # keep going, drop broken images
		print(f"    ! image failed {candidate}: {exc}")
		cache[src] = ""
		return ""
	name = os.path.basename(urllib.parse.urlparse(candidate).path)
	ext = os.path.splitext(name)[1].lower() or ".jpg"
	local = os.path.join(CACHE, hashlib.sha256(src.encode()).hexdigest()[:16] + ext)
	open(local, "wb").write(data)
	url = upload_file(local)
	cache[src] = url
	return url


_ATTACHMENTS: dict[str, str] = {}


def localise_attachment(href: str) -> str:
	"""Copy a Shopify-hosted document (PDF etc.) into ERPNext /files."""
	path = urllib.parse.urlparse(href).path
	if path in _ATTACHMENTS:
		return _ATTACHMENTS[path]
	url = href if href.startswith("http") else SHOPIFY + path
	try:
		data = get(url)
	except Exception as exc:
		print(f"    ! attachment failed {url}: {exc}")
		_ATTACHMENTS[path] = href
		return href
	name = os.path.basename(path)
	local = os.path.join(CACHE, hashlib.sha256(url.encode()).hexdigest()[:16] + os.path.splitext(name)[1])
	open(local, "wb").write(data)
	_ATTACHMENTS[path] = upload_file(local)
	return _ATTACHMENTS[path]


def rewrite_href(href: str) -> str:
	if not href:
		return href
	href = href.strip()
	if href.startswith(("mailto:", "tel:", "#")):
		return href
	if href.startswith("//"):
		href = "https:" + href
	if href.startswith(SHOPIFY):
		href = href[len(SHOPIFY):] or "/"
	path = urllib.parse.urlparse(href).path
	query = urllib.parse.urlparse(href).query
	handle = path.strip("/")
	if path.startswith("/pages/"):
		return "/" + handle.split("/", 1)[1] + (f"?{query}" if query else "")
	if path.startswith("/products/"):
		return "/" + handle.split("/", 1)[1] + (f"?{query}" if query else "")
	if path.startswith("/collections/"):
		return "/all-products"
	if path.startswith("/blogs/"):
		parts = path.strip("/").split("/")
		if len(parts) >= 3:
			return f"/blog/{parts[1]}/{parts[2]}"
		return "/blog"
	if href.startswith("https://cdn.shopify.com/files") or href.startswith("https://cdn.shopify.com/s/"):
		return localise_attachment(href)
	if path.startswith("/s/files/"):
		return localise_attachment(href)
	if path.startswith("/cdn/"):
		return href
	return href


def walk(node, out: list, images: dict, image_cache: dict) -> None:
	"""Emit semantic HTML, dropping Shopify layout wrappers."""
	for child in node.children:
		name = getattr(child, "name", None)
		if name is None:
			text = str(child)
			if text.strip():
				out.append(escape_text(text))
			continue
		name = name.lower()
		if name in DROP:
			# noscript still tells us the real image, harvest it first
			if name == "noscript":
				harvest_noscript(child, out, images, image_cache)
			continue
		if name == "img":
			src = child.get("src") or child.get("data-src") or child.get("data-original")
			if not src and child.get("srcset"):
				src = child["srcset"].split(",")[0].strip().split()[0]
			url = cdn_url(src)
			if url:
				local = localise_image(url, image_cache)
				if local:
					alt = escape_attr(child.get("alt") or "")
					out.append(f'<img src="{local}" alt="{alt}" loading="lazy">')
			continue
		if name == "a":
			href = rewrite_href(child.get("href") or "")
			inner: list = []
			walk(child, inner, images, image_cache)
			body = "".join(inner).strip() or escape_text(child.get_text(" ", strip=True))
			if body:
				out.append(f'<a href="{escape_attr(href)}">{body}</a>')
			continue
		if name in ("div", "span", "section", "article", "aside", "main",
		            "figure", "header", "footer", "small", "label", "time"):
			# harvest lazyload background images before unwrapping
			bgset = child.get("data-bgset") or child.get("data-bg")
			if bgset and child.find("img") is None:
				url = cdn_url(bgset)
				if url:
					local = localise_image(url, image_cache)
					if local:
						alt = escape_attr(child.get("data-alt") or child.get("aria-label") or "")
						out.append(f'<img src="{local}" alt="{alt}" loading="lazy">')
			walk(child, out, images, image_cache)
			continue
		if name in ALLOWED:
			inner = []
			walk(child, inner, images, image_cache)
			body = "".join(inner).strip()
			if not body and name != "hr":
				continue
			attrs = ""
			if name == "img":
				pass
			elif name == "a":
				attrs = f' href="{escape_attr(rewrite_href(child.get("href") or ""))}"'
			out.append(f"<{name}{attrs}>{body}</{name}>" if name != "hr" else "<hr>")
			continue
		# unknown tag: recurse so we never lose text
		walk(child, out, images, image_cache)


def harvest_noscript(node, out: list, images: dict, image_cache: dict) -> None:
	img = node.find("img")
	if not img:
		return
	url = cdn_url(img.get("src") or img.get("srcset", ""))
	if not url:
		return
	local = localise_image(url, image_cache)
	if local:
		out.append(f'<img src="{local}" alt="{escape_attr(img.get("alt") or "")}" loading="lazy">')


def escape_text(text: str) -> str:
	return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def escape_attr(text: str) -> str:
	return escape_text(text).replace('"', "&quot;")


def group_people(html: str) -> str:
	"""Turn <img><h2>Name</h2><p>Role</p> triples into a person card."""
	pattern = re.compile(
		r'(<img src="[^"]+" alt="[^"]*"[^>]*>)<h2>(.*?)</h2><p>(.*?)</p>', re.S
	)
	return pattern.sub(
		lambda m: (
			'<figure class="glia-person">' + m.group(1)
			+ f"<figcaption><h3>{m.group(2)}</h3><p>{m.group(3)}</p></figcaption></figure>"
		),
		html,
	)


WIDGET_NOISE = re.compile(
	r"<h2>\s*Glia4Gaza\s*[-–]\s*(?:One-Time|Monthly) Gift\s*</h2>"
	r"|<p>\s*Total Donation\s*\$[\d.,]+\s*CAD\s*</p>"
	r"|<h4>\s*Monthly Gift\s*</h4>\s*<p>\s*Turn one day of solidarity into ongoing support\.\s*</p>",
	re.I,
)

CTA = ('<div class="glia-page-cta">'
       '<a class="glia-btn glia-btn--primary" href="/donate-glia-project">Donate once</a>'
       '<a class="glia-btn" href="/support-glias-mission">Give monthly</a>'
       '</div>')


def tidy(html: str) -> str:
	html = re.sub(r"\s+", " ", html)
	html = re.sub(r"(<br>\s*){3,}", "<br><br>", html)
	html = re.sub(r"<(p|h[1-5]|ul|ol|blockquote|figure|table)>\s*</\1>", "", html)
	html = re.sub(r"\s*(<(?:p|h[1-5]|ul|ol|li|blockquote|figure|table)[ >])", r"\1", html)
	# keep paragraphs as blocks for readable spacing
	html = html.replace(" <p", "<p").replace(" <h", "<h").replace(" <ul", "<ul")
	return html.strip()


def scrape(handle: str, dry_run: bool) -> dict:
	page_name, route = PAGES[handle]
	print(f"\n== {handle} -> Web Page {page_name} (route /{route})")
	html = get(f"{SHOPIFY}/pages/{handle}").decode("utf-8", "replace")
	soup = BeautifulSoup(html, "lxml")
	main = soup.find("main", id="MainContent") or soup.find("main")
	if main is None:
		raise SystemExit(f"no <main> on {handle}")
	# Shopify leaves <!-- /sections/section-text.liquid --> breadcrumbs that would
	# otherwise be emitted as visible text.
	from bs4 import Comment
	for node in main.find_all(string=lambda t: isinstance(t, Comment)):
		node.extract()
	# Shopify keeps the real <img> inside <noscript> for lazyloaded cards; lift it
	# out so it participates in the walk (and carries the alt text).
	for noscript in main.find_all("noscript"):
		img = noscript.find("img")
		if img is not None:
			noscript.replace_with(img)
		else:
			noscript.decompose()
	title_tag = soup.find("title")
	title = re.sub(r"\s*[|–—-]\s*Glia.*$", "", title_tag.get_text(strip=True)) if title_tag else page_name

	out: list = []
	image_cache: dict = {}
	walk(main, out, {}, image_cache)
	body = tidy("".join(out))
	body = WIDGET_NOISE.sub("", body)
	if handle in ("glia4gaza", "run-for-palestine-2026"):
		# the Shopify donation app widgets cannot come across; give the donor our
		# own paths instead of a dead form
		body = body.replace("<h1>", CTA + "<h1>", 1) if "<h1>" in body else CTA + body
	if handle == "meet-the-team":
		body = group_people(body)
		# the lazyload background image and the noscript <img> describe the same
		# portrait; keep the one with alt text
		body = re.sub(r'<img src="[^"]+" alt="" loading="lazy">(?=<figure class="glia-person">)', "", body)
	text = re.sub(r"<[^>]+>", " ", body)
	text = re.sub(r"\s+", " ", text).strip()

	result = {
		"handle": handle,
		"page_name": page_name,
		"route": route,
		"title": title,
		"images": len(re.findall(r"<img", body)),
		"visible_chars": len(text),
		"html_chars": len(body),
	}
	if dry_run:
		result["sample"] = body[:1200]
		return result

	# write into the Web Page
	payload = {
		"main_section_html": f'<div class="glia-page-content">{body}</div>',
		"title": title,
		"published": 1,
	}
	req = urllib.request.Request(
		f"{ERPNEXT}/api/resource/Web%20Page/{urllib.parse.quote(page_name)}",
		data=json.dumps(payload).encode(),
		method="PUT",
		headers={"Content-Type": "application/json", "X-Frappe-CSRF-Token": "None",
		         "Cookie": read_cookie()},
	)
	try:
		urllib.request.urlopen(req, timeout=120)
	except Exception as exc:
		result["error"] = str(exc)
		return result
	result["written"] = True
	return result


def read_cookie() -> str:
	"""Turn a curl cookie jar into a Cookie header."""
	parts = []
	for line in open(COOKIE_JAR, encoding="utf-8"):
		# HttpOnly cookies are written with a "#HttpOnly_" prefix; keep those.
		if line.startswith("#HttpOnly_"):
			line = line[len("#HttpOnly_"):]
		elif line.startswith("#") or not line.strip():
			continue
		fields = line.strip().split("\t")
		if len(fields) >= 7:
			parts.append(f"{fields[5]}={fields[6]}")
	return "; ".join(parts)


def main() -> None:
	ap = argparse.ArgumentParser()
	ap.add_argument("handles", nargs="*", default=list(PAGES))
	ap.add_argument("--dry-run", action="store_true")
	args = ap.parse_args()
	handles = args.handles or list(PAGES)
	results = []
	for handle in handles:
		try:
			results.append(scrape(handle, args.dry_run))
		except SystemExit as exc:
			results.append({"handle": handle, "error": str(exc)})
	json.dump(results, open("/tmp/scrape_results.json", "w"), indent=1)
	for r in results:
		if r.get("error"):
			print(f"  {r['handle']:32s} ERROR {r['error']}")
		else:
			print(f"  {r['handle']:32s} chars={r['visible_chars']:6d} imgs={r['images']:3d} "
			      f"{'written' if r.get('written') else 'dry-run'}")


if __name__ == "__main__":
	main()

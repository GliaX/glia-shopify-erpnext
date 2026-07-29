"""Config tests: YAML parsing, derived GID sets, secrets stay secret."""

from __future__ import annotations

from glia_shopify_sync.config import AppConfig, Settings, load_yaml_config


def _write_config(tmp_path, body: str):
    p = tmp_path / "config.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_yaml_config_loads_allowlist_and_derives_gids(tmp_path):
    p = _write_config(
        tmp_path,
        """
company: Glia
default_currency: CAD
donation_product_ids:
  - "7962927005795"
  - "7967052890211"
recurring_product_ids:
  - "7962927038563"
""",
    )
    cfg = AppConfig(settings=Settings(_env_file=None), yaml_cfg=load_yaml_config(p))

    assert cfg.yaml.company == "Glia"
    assert cfg.yaml.default_currency == "CAD"
    assert cfg.donation_product_gids == {
        "gid://shopify/Product/7962927005795",
        "gid://shopify/Product/7967052890211",
    }
    assert cfg.recurring_product_gids == {"gid://shopify/Product/7962927038563"}


def test_yaml_config_defaults_when_omitted(tmp_path):
    p = _write_config(tmp_path, 'company: "Glia"\n')
    cfg = load_yaml_config(p)
    assert cfg.tip_mode == "fold"
    assert cfg.sync.page_size == 250
    assert cfg.sync.paid_only is True
    assert cfg.backfill.since == "2020-01-01"


def test_secret_str_not_leaked_in_repr():
    s = Settings(_env_file=None, shopify_client_secret="shpss_super_secret_value")
    assert "shpss_super_secret_value" not in repr(s)

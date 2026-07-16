#!/usr/bin/env python3
"""
Create missing Grafana account panels from the account config.

By default the script only appends panels whose titles do not exist. Existing
panels, including manually edited or manually created panels, are left intact.
Use --force-rebuild only for an explicit full rebuild.

Default behavior writes to the two lljtest dashboards. Use --dry-run only when
you want to preview counts without saving.
"""

import argparse
import copy
import json
import sys
from pathlib import Path

import requests

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import (
    ACCOUNTS_CONFIG_PATH,
    ANNUALIZED_DASHBOARD_UID,
    GRAFANA_API_TOKEN,
    GRAFANA_PASSWORD,
    GRAFANA_PROMETHEUS_INSTANCE_ACCOUNT_MONITOR,
    GRAFANA_PROMETHEUS_UID_ACCOUNT_MONITOR,
    GRAFANA_URL,
    GRAFANA_USER,
    NAV_DASHBOARD_UID,
    REFERENCE_ANNUALIZED_DASHBOARD_UID,
    REFERENCE_NAV_DASHBOARD_UID,
    environment_value,
)

CONFIG_PATH = ACCOUNTS_CONFIG_PATH

# IMPORTANT:
# Default panel count comes from accounts_config.yaml.
# Data source defaults to the first Prometheus data source. To force the account
# monitor data source, set GRAFANA_PROMETHEUS_UID_ACCOUNT_MONITOR.
SOURCE_CONFIGS = [
    {
        "source": "account_monitor",
        "exchange": "",
        "panel_exchange": "",
        "metric_prefix": "",
        "instance": GRAFANA_PROMETHEUS_INSTANCE_ACCOUNT_MONITOR,
        "config_path": CONFIG_PATH,
        "datasource_uid": GRAFANA_PROMETHEUS_UID_ACCOUNT_MONITOR,
        "datasource_env": "GRAFANA_PROMETHEUS_UID_ACCOUNT_MONITOR",
        "instance_env": "GRAFANA_PROMETHEUS_INSTANCE_ACCOUNT_MONITOR",
    },
]


ANNUALIZED_PANEL_STYLE = {
    "type": "table",
    "pluginVersion": "12.0.0",
    "gridPos": {"x": 0, "y": 0, "h": 4, "w": 12},
    "fieldConfig": {
        "defaults": {
            "color": {"mode": "thresholds"},
            "custom": {
                "align": "auto",
                "cellOptions": {"type": "color-text"},
                "inspect": False,
                "width": 100,
            },
            "decimals": 2,
            "fieldMinMax": False,
            "mappings": [],
            "thresholds": {
                "mode": "absolute",
                "steps": [
                    {"color": "red"},
                    {"color": "green", "value": 0},
                ],
            },
        },
        "overrides": [
            {
                "matcher": {"id": "byName", "options": "Date"},
                "properties": [{"id": "custom.width", "value": 150}],
            },
            {
                "matcher": {"id": "byName", "options": "Actual Equity"},
                "properties": [
                    {"id": "unit", "value": "none"},
                ],
            },
            {
                "matcher": {"id": "byRegexp", "options": ".*AR.*"},
                "properties": [
                    {"id": "unit", "value": "percent"},
                ],
            }
        ],
    },
    "options": {
        "cellHeight": "sm",
        "footer": {
            "countRows": False,
            "fields": "",
            "reducer": ["sum"],
            "show": False,
        },
        "showHeader": True,
    },
    "transformations": [
        {"id": "merge", "options": {}},
        {
            "id": "organize",
            "options": {
                "excludeByName": {},
                "includeByName": {},
                "indexByName": {},
                "renameByName": {
                    "Time": "Date",
                    "Value": "1-Day AR(%)",
                },
            },
        },
    ],
}


NAV_PANEL_STYLE = {
    "type": "timeseries",
    "pluginVersion": "12.0.0",
    "gridPos": {"x": 0, "y": 0, "h": 8, "w": 12},
    "fieldConfig": {
        "defaults": {
            "color": {"mode": "palette-classic"},
            "custom": {
                "axisBorderShow": False,
                "axisCenteredZero": False,
                "axisColorMode": "text",
                "axisLabel": "",
                "axisPlacement": "auto",
                "barAlignment": 0,
                "barWidthFactor": 0.6,
                "drawStyle": "line",
                "fillOpacity": 0,
                "gradientMode": "none",
                "hideFrom": {"legend": False, "tooltip": False, "viz": False},
                "insertNulls": False,
                "lineInterpolation": "linear",
                "lineWidth": 1,
                "pointSize": 5,
                "scaleDistribution": {"type": "linear"},
                "showPoints": "never",
                "spanNulls": True,
                "stacking": {"group": "A", "mode": "none"},
                "thresholdsStyle": {"mode": "off"},
            },
            "decimals": 8,
            "displayName": "Actual equity",
            "mappings": [],
            "thresholds": {
                "mode": "absolute",
                "steps": [
                    {"color": "green"},
                    {"color": "red", "value": 80},
                ],
            },
        },
        "overrides": [],
    },
    "options": {
        "legend": {
            "calcs": ["lastNotNull"],
            "displayMode": "list",
            "placement": "bottom",
            "showLegend": True,
        },
        "tooltip": {"hideZeros": False, "mode": "single", "sort": "none"},
    },
}


def main():
    args = parse_args()
    client = GrafanaClient.from_env()

    if args.dump_reference_style:
        dump_reference_style(client, args)
        return

    sync_dashboards(
        client=client,
        config_args=args.config,
        annualized_uid=args.annualized_uid,
        nav_uid=args.nav_uid,
        dry_run=args.dry_run,
        force_rebuild=args.force_rebuild,
        logger=None,
    )


def sync_dashboards(
    client=None,
    config_args=None,
    annualized_uid=ANNUALIZED_DASHBOARD_UID,
    nav_uid=NAV_DASHBOARD_UID,
    dry_run=False,
    force_rebuild=False,
    account_names=None,
    logger=None,
):
    accounts = load_accounts(config_args or [])
    if account_names:
        selected_names = set(account_names)
        accounts = [account for account in accounts if account["name"] in selected_names]
    client = client or GrafanaClient.from_env()
    datasource_by_source = resolve_datasources(client, accounts)
    annualized_response = client.get_dashboard(annualized_uid)
    nav_response = client.get_dashboard(nav_uid)
    annualized_style = copy.deepcopy(ANNUALIZED_PANEL_STYLE)
    nav_style = copy.deepcopy(NAV_PANEL_STYLE)

    annualized_panels = build_annualized_panels(accounts, datasource_by_source, annualized_style)
    nav_panels = build_nav_panels(accounts, datasource_by_source, nav_style)

    annualized_panels, annualized_added = panels_for_save(
        annualized_response, annualized_panels, force_rebuild
    )
    nav_panels, nav_added = panels_for_save(nav_response, nav_panels, force_rebuild)

    emit(logger, "info", "accounts: %s", len(accounts))
    mode = "force rebuild" if force_rebuild else "append only"
    emit(logger, "info", "sync mode: %s", mode)
    emit(logger, "info", "annualized dashboard uid: %s, total panels: %s, added: %s", annualized_uid, len(annualized_panels), annualized_added)
    emit(logger, "info", "nav dashboard uid: %s, total panels: %s, added: %s", nav_uid, len(nav_panels), nav_added)
    emit(logger, "info", "annualized style panel: %s", style_summary(annualized_style))
    emit(logger, "info", "nav style panel: %s", style_summary(nav_style))
    emit(logger, "info", "datasources:")
    for source, datasource in datasource_by_source.items():
        emit(logger, "info", "  %s: %s", source, datasource)

    if dry_run:
        emit(logger, "info", "dry-run only; no dashboard was saved")
        return {"annualized_added": annualized_added, "nav_added": nav_added}

    if not force_rebuild and annualized_added == 0 and nav_added == 0:
        emit(logger, "info", "all account panels already exist; no dashboard was saved")
        return {"annualized_added": 0, "nav_added": 0}

    if force_rebuild or annualized_added:
        save_generated_dashboard(
            client,
            annualized_response,
            annualized_panels,
            "force rebuild annualized panels" if force_rebuild else "append missing annualized panels",
        )
    if force_rebuild or nav_added:
        save_generated_dashboard(
            client,
            nav_response,
            nav_panels,
            "force rebuild nav panels" if force_rebuild else "append missing nav panels",
        )
    emit(logger, "info", "dashboard changes saved")
    return {"annualized_added": annualized_added, "nav_added": nav_added}


def emit(logger, level, message, *args):
    if logger:
        getattr(logger, level)(message, *args)
    else:
        print(message % args if args else message)


def parse_args():
    parser = argparse.ArgumentParser(description="Generate Grafana account panels.")
    parser.add_argument(
        "--config",
        action="append",
        default=[],
        help=(
            "Optional config source in source:exchange:metric_prefix:path format. "
            "By default the script reads this project's accounts_config.yaml."
        ),
    )
    parser.add_argument("--annualized-uid", default=ANNUALIZED_DASHBOARD_UID)
    parser.add_argument("--nav-uid", default=NAV_DASHBOARD_UID)
    parser.add_argument("--reference-annualized-uid", default=REFERENCE_ANNUALIZED_DASHBOARD_UID)
    parser.add_argument("--reference-nav-uid", default=REFERENCE_NAV_DASHBOARD_UID)
    parser.add_argument(
        "--dump-reference-style",
        action="store_true",
        help="Print reusable style JSON from the old dashboards without saving target dashboards",
    )
    parser.add_argument(
        "--reference-style-output",
        default="",
        help="Optional file path for --dump-reference-style JSON output",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview generated panel counts without saving")
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help="Replace every panel in both dashboards. Without this flag, only missing account panels are appended.",
    )
    args = parser.parse_args()
    return args


class GrafanaClient:
    def __init__(self, base_url, headers, auth=None):
        self.base_url = base_url.rstrip("/")
        self.headers = headers
        self.auth = auth

    @classmethod
    def from_env(cls):
        base_url = GRAFANA_URL
        token = GRAFANA_API_TOKEN
        user = GRAFANA_USER
        password = GRAFANA_PASSWORD
        headers = {"Content-Type": "application/json"}
        if token:
            try:
                token.encode("ascii")
            except UnicodeEncodeError as exc:
                raise RuntimeError(
                    "GRAFANA_API_TOKEN contains non-ASCII characters. "
                    "Use the actual Grafana service account token value, usually starting with 'glsa_', "
                    "not the service account/token display name."
                ) from exc
            headers["Authorization"] = f"Bearer {token}"
            return cls(base_url, headers)
        if user and password:
            return cls(base_url, headers, auth=(user, password))
        print("No Grafana credentials set; trying anonymous dashboard write access.")
        return cls(base_url, headers)

    def get_dashboard(self, uid):
        response = requests.get(
            f"{self.base_url}/api/dashboards/uid/{uid}",
            headers=self.headers,
            auth=self.auth,
            timeout=15,
        )
        response.raise_for_status()
        return response.json()

    def save_dashboard(self, payload):
        response = requests.post(
            f"{self.base_url}/api/dashboards/db",
            headers=self.headers,
            auth=self.auth,
            data=json.dumps(payload),
            timeout=15,
        )
        response.raise_for_status()
        return response.json()

    def list_datasources(self):
        response = requests.get(
            f"{self.base_url}/api/datasources",
            headers=self.headers,
            auth=self.auth,
            timeout=15,
        )
        response.raise_for_status()
        return response.json()


def load_accounts(config_args):
    accounts = []
    for source_config in source_configs_from_args(config_args):
        accounts.extend(load_accounts_from_source(source_config))
    return sorted(accounts, key=lambda item: (item["source"], item["name"]))


def source_configs_from_args(config_args):
    if not config_args:
        return SOURCE_CONFIGS
    configs = []
    for config_arg in config_args:
        parts = config_arg.split(":", 3)
        if len(parts) != 4:
            raise ValueError("--config must use source:exchange:metric_prefix:path format")
        source, exchange, metric_prefix, config_path = parts
        configs.append(
            {
                "source": source,
                "exchange": exchange,
                "metric_prefix": metric_prefix,
                "config_path": Path(config_path),
                "datasource_env": f"GRAFANA_PROMETHEUS_UID_{source.upper()}",
            }
        )
    return configs


def load_accounts_from_source(source_config):
    with open(source_config["config_path"], "r", encoding="utf-8") as file:
        raw_config = load_yaml_mapping(file.read())

    accounts = []
    for account_name, account_info in raw_config.items():
        if not isinstance(account_info, dict):
            continue
        if "initial_unit" not in account_info:
            continue
        accounts.append(
            {
                "name": account_name,
                "ccy": str(account_info.get("ccy", "USDT")).upper(),
                "client": account_info.get("client", ""),
                "exchange": account_exchange(account_info, source_config),
                "panel_exchange": panel_exchange(account_info, source_config),
                "source": source_config["source"],
                "metric_prefix": metric_prefix(account_info, source_config),
                "instance": source_instance(source_config),
            }
        )
    return accounts


def account_exchange(account_info, source_config):
    return account_info.get("exchange") or source_config.get("exchange") or "Binance"


def metric_prefix(account_info, source_config):
    if source_config.get("metric_prefix"):
        return source_config["metric_prefix"]
    exchange = account_exchange(account_info, source_config)
    return exchange[:1].upper() + exchange[1:].lower()


def panel_exchange(account_info, source_config):
    if source_config.get("panel_exchange"):
        return source_config["panel_exchange"]
    exchange = account_exchange(account_info, source_config).strip().lower()
    if exchange == "binance":
        return "BN"
    if exchange == "gate":
        return "Gate"
    return exchange[:1].upper() + exchange[1:]


def source_instance(source_config):
    env_name = source_config.get("instance_env")
    if env_name:
        configured_value = environment_value(env_name)
        if configured_value:
            return configured_value
    return source_config.get("instance", "")


def load_yaml_mapping(content):
    try:
        import yaml

        return yaml.safe_load(content) or {}
    except ModuleNotFoundError:
        return load_simple_yaml_mapping(content)


def load_simple_yaml_mapping(content):
    """Parse the simple top-level account YAML shape used by legacy configs."""
    result = {}
    current_key = None
    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")) and line.endswith(":"):
            current_key = line[:-1].strip()
            result[current_key] = {}
            continue
        if current_key is None or not line.startswith((" ", "\t")) or ":" not in line:
            continue
        key, value = line.strip().split(":", 1)
        result[current_key][key.strip()] = parse_simple_yaml_value(value.strip())
    return result


def parse_simple_yaml_value(value):
    if value == "":
        return ""
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value.strip("'\"")


def resolve_datasources(client, accounts):
    fallback_uid = first_prometheus_datasource_uid(client)
    datasource_by_source = {}
    for source_config in SOURCE_CONFIGS:
        source = source_config["source"]
        datasource_uid = source_config.get("datasource_uid")
        if not datasource_uid:
            datasource_uid = environment_value(source_config["datasource_env"])
        datasource_uid = datasource_uid or fallback_uid
        datasource_by_source[source] = prometheus_datasource(datasource_uid)
    for account in accounts:
        if account["source"] not in datasource_by_source:
            datasource_by_source[account["source"]] = prometheus_datasource(fallback_uid)
    return datasource_by_source


def first_prometheus_datasource_uid(client):
    datasources = client.list_datasources()
    for datasource in datasources:
        if datasource.get("type") == "prometheus":
            return datasource["uid"]
    raise RuntimeError("No Prometheus datasource found in Grafana.")


def prometheus_datasource(uid):
    return {"type": "prometheus", "uid": uid}


def datasource_for_account(account, datasource_by_source):
    return datasource_by_source[account["source"]]


def load_reference_style(client, dashboard_uid, purpose, preferred_type=None):
    dashboard = client.get_dashboard(dashboard_uid)["dashboard"]
    panels = list(iter_reference_panels(dashboard.get("panels", [])))
    style_panel = choose_style_panel(panels, preferred_type)
    if style_panel is None:
        print(f"reference {purpose} dashboard has no reusable panel; using built-in style")
        return None
    return style_panel


def iter_reference_panels(panels):
    for panel in panels:
        if panel.get("type") != "row":
            yield panel
        for nested_panel in iter_reference_panels(panel.get("panels") or []):
            yield nested_panel


def choose_style_panel(panels, preferred_type=None):
    if preferred_type:
        for panel in panels:
            if panel.get("type") == preferred_type:
                return panel
    preferred_types = ("timeseries", "graph", "stat", "gauge", "bargauge")
    for panel_type in preferred_types:
        for panel in panels:
            if panel.get("type") == panel_type:
                return panel
    return panels[0] if panels else None


def style_summary(style_panel):
    if not style_panel:
        return "built-in"
    return f"type={style_panel.get('type')}, title={style_panel.get('title')!r}"


def panel_title(account):
    display_account = account["name"].replace("_", "")
    return f"{account['panel_exchange']}_{display_account}_{account['ccy']}"


def nav_panel_title(account):
    display_account = account["name"].replace("_", "")
    title_parts = [account["panel_exchange"], display_account]
    if account["name"].upper().startswith("BV_") and account.get("client"):
        title_parts.append(str(account["client"]).upper())
    title_parts.append(account["ccy"])
    return "_".join(title_parts)


def dump_reference_style(client, args):
    annualized_style = load_reference_style(client, args.reference_annualized_uid, "annualized", "table")
    nav_style = load_reference_style(client, args.reference_nav_uid, "nav", "timeseries")
    payload = {
        "annualized_style": sanitize_style_for_hardcoding(annualized_style),
        "nav_style": sanitize_style_for_hardcoding(nav_style),
    }
    output = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.reference_style_output:
        with open(args.reference_style_output, "w", encoding="utf-8", newline="\n") as file:
            file.write(output)
            file.write("\n")
        print(f"wrote reference style JSON to {args.reference_style_output}")
    else:
        print(output)


def sanitize_style_for_hardcoding(style_panel):
    if style_panel is None:
        return None
    panel = copy.deepcopy(style_panel)
    remove_old_panel_state(panel)
    for key in ("datasource", "gridPos", "id", "targets", "title"):
        panel.pop(key, None)
    return panel


def build_annualized_panels(accounts, datasource_by_source, style_panel):
    panels = []
    for index, account in enumerate(accounts):
        panels.append(
            timeseries_panel(
                panel_id=index + 1,
                title=panel_title(account),
                grid_pos=grid_pos(index, style_panel),
                datasource=datasource_for_account(account, datasource_by_source),
                targets=[
                    annualized_prom_target(account, "actual_equity", "Actual Equity", "A"),
                    annualized_prom_target(account, "annualized_return_24h", "1-Day AR(%)", "C"),
                    annualized_prom_target(account, "annualized_return_7d", "7-Day AR(%)", "D"),
                    annualized_prom_target(account, "annualized_return_30d", "30-Day AR(%)", "E"),
                    annualized_prom_target(account, "annualized_return_1m", "24h AR(%)", "F"),
                    annualized_prom_target(account, "annualized_return_1h", "24h MED-AR(%)", "B"),
                ],
                unit="percent",
                style_panel=style_panel,
            )
        )
    return panels


def build_nav_panels(accounts, datasource_by_source, style_panel):
    panels = []
    for index, account in enumerate(accounts):
        panel = timeseries_panel(
            panel_id=index + 1,
            title=nav_panel_title(account),
            grid_pos=grid_pos(index, style_panel),
            datasource=datasource_for_account(account, datasource_by_source),
            targets=[nav_prom_target(account)],
            unit="none",
            style_panel=style_panel,
        )
        for target in panel["targets"]:
            for key in ("datasource", "exemplar", "hide", "instant"):
                target.pop(key, None)
        panels.append(panel)
    return panels


def timeseries_panel(panel_id, title, grid_pos, datasource, targets, unit, style_panel=None):
    for target in targets:
        target["datasource"] = datasource

    if style_panel:
        panel = copy.deepcopy(style_panel)
        remove_old_panel_state(panel)
    else:
        panel = builtin_timeseries_panel(unit)

    panel["id"] = panel_id
    panel["title"] = title
    panel["datasource"] = datasource
    panel["gridPos"] = grid_pos
    panel["targets"] = targets
    set_panel_unit(panel, unit)
    set_table_transform_renames(panel, targets)
    for target in targets:
        target.pop("_displayName", None)
        target.pop("_metricName", None)
    return panel


def remove_old_panel_state(panel):
    for key in (
        "alert",
        "alerting",
        "collapsed",
        "id",
        "libraryPanel",
        "panels",
        "repeat",
        "repeatDirection",
        "repeatPanelId",
        "scopedVars",
    ):
        panel.pop(key, None)


def set_panel_unit(panel, unit):
    field_config = panel.setdefault("fieldConfig", {})
    defaults = field_config.setdefault("defaults", {})
    if unit == "none":
        defaults.pop("unit", None)
    else:
        defaults["unit"] = unit


def prom_target(account, suffix, legend, ref_id, instant=False):
    metric = f"{account['metric_prefix']}_{account['name']}_{suffix}"
    expr = metric_expr(metric, account.get("instance", ""))
    return {
        "datasource": None,
        "disableTextWrap": False,
        "editorMode": "builder" if instant else "code",
        "exemplar": False,
        "expr": expr,
        "fullMetaSearch": False,
        "hide": False,
        "includeNullMetadata": True,
        "instant": instant,
        "legendFormat": legend,
        "range": not instant,
        "refId": ref_id,
        "useBackend": False,
        "_displayName": legend,
        "_metricName": metric,
    }


def nav_prom_target(account):
    target = prom_target(account, "actual_equity", "__auto", "A", instant=False)
    series = target["expr"]
    target["expr"] = (
        f"(\n  {series} > 1e-7\n)\n"
        "and\n"
        f"(\n  {series}\n"
        "  >=\n"
        "  quantile_over_time(\n"
        "    0.01,\n"
        f"    {series}[$__range] @ end()\n"
        "  )\n"
        ")\n"
        "and\n"
        f"(\n  {series}\n"
        "  <=\n"
        "  quantile_over_time(\n"
        "    0.99,\n"
        f"    {series}[$__range] @ end()\n"
        "  )\n"
        ")"
    )
    target["editorMode"] = "code"
    return target


def annualized_prom_target(account, suffix, display_name, ref_id):
    target = prom_target(account, suffix, display_name, ref_id, instant=True)
    series = target["expr"]
    infinite = f"abs({series}) == +Inf"
    target["expr"] = (
        f"({series} unless ({infinite}))\n"
        f"or (({infinite}) * 0 / 0)"
    )
    target["editorMode"] = "code"
    target["legendFormat"] = display_name
    return target


def metric_expr(metric, instance):
    if instance:
        return f'{metric}{{instance="{instance}"}}'
    return metric


def set_table_transform_renames(panel, targets):
    if panel.get("type") != "table":
        return
    rename_by_name = {
        "Time": "Date",
        "Value": targets[0].get("_displayName", "Value") if targets else "Value",
    }
    for target in targets:
        expr = target["expr"]
        metric = target.get("_metricName", expr)
        legend = target.get("_displayName", target.get("legendFormat", metric))
        rename_by_name[expr] = legend
        rename_by_name[metric] = legend
        rename_by_name[f'{{__name__="{metric}"}}'] = legend
        rename_by_name.update(labelled_series_renames(metric, legend))

    transformations = panel.setdefault("transformations", [])
    organize = None
    for transformation in transformations:
        if transformation.get("id") == "organize":
            organize = transformation
            break
    if organize is None:
        organize = {"id": "organize", "options": {}}
        transformations.append(organize)
    options = organize.setdefault("options", {})
    options.setdefault("excludeByName", {})
    options["includeByName"] = {}
    options["indexByName"] = {}
    options["renameByName"] = rename_by_name
    for target in targets:
        target.pop("_displayName", None)
        target.pop("_metricName", None)


def table_include_by_name(targets):
    include = {"Time": True, "Date": True}
    for target in targets:
        expr = target["expr"]
        metric = target.get("_metricName", expr)
        legend = target.get("_displayName", target.get("legendFormat", metric))
        include[legend] = True
        include[expr] = True
        include[metric] = True
        include[f'{{__name__="{metric}"}}'] = True
        for series_name in labelled_series_renames(metric, legend):
            include[series_name] = True
    return include


def table_index_by_name(targets):
    order = {"Time": 0, "Date": 0}
    for index, target in enumerate(targets, start=1):
        expr = target["expr"]
        metric = target.get("_metricName", expr)
        legend = target.get("_displayName", target.get("legendFormat", metric))
        order[legend] = index
        order[expr] = index
        order[metric] = index
        order[f'{{__name__="{metric}"}}'] = index
        for series_name in labelled_series_renames(metric, legend):
            order[series_name] = index
    return order


def labelled_series_renames(metric, legend):
    jobs = (
        "account-monitor",
        "binance-actual-equity",
        "binance-b-actual-equity",
        "gate-actual-equity",
    )
    instances = (
        "localhost:7007",
        "localhost:8005",
        "localhost:8020",
    )
    renames = {}
    for instance in instances:
        for job in jobs:
            renames[f'{{__name__="{metric}", instance="{instance}", job="{job}"}}'] = legend
            renames[f'{{__name__="{metric}", job="{job}", instance="{instance}"}}'] = legend
    return renames


def builtin_timeseries_panel(unit):
    return {
        "type": "timeseries",
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {
                    "drawStyle": "line",
                    "lineInterpolation": "linear",
                    "lineWidth": 1,
                    "fillOpacity": 8,
                    "showPoints": "never",
                    "spanNulls": True,
                },
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"color": "green", "value": None},
                        {"color": "red", "value": 80},
                    ],
                },
            },
            "overrides": [],
        },
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "none"},
        },
    }


def grid_pos(index, style_panel=None):
    template_pos = (style_panel or {}).get("gridPos") or {}
    is_table = (style_panel or {}).get("type") == "table"
    width = int(template_pos.get("w", 12))
    default_height = 4 if is_table else 8
    height = int(template_pos.get("h", default_height))
    width = max(1, min(width, 24))
    height = max(1, height)
    columns = max(1, 24 // width)
    return {
        "x": (index % columns) * width,
        "y": (index // columns) * height,
        "w": width,
        "h": height,
    }


def panels_for_save(dashboard_response, generated_panels, force_rebuild=False):
    """Return the panels to save and the number of generated panels added."""
    if force_rebuild:
        return copy.deepcopy(generated_panels), len(generated_panels)

    existing_panels = copy.deepcopy(dashboard_response["dashboard"].get("panels") or [])
    existing_titles = {
        panel.get("title") for panel in existing_panels if panel.get("title")
    }
    missing_panels = [
        copy.deepcopy(panel)
        for panel in generated_panels
        if panel.get("title") not in existing_titles
    ]
    if not missing_panels:
        return existing_panels, 0

    next_id = max(
        (panel.get("id", 0) for panel in existing_panels if isinstance(panel.get("id"), int)),
        default=0,
    ) + 1
    next_y = max(
        (
            int(panel.get("gridPos", {}).get("y", 0))
            + int(panel.get("gridPos", {}).get("h", 0))
            for panel in existing_panels
        ),
        default=0,
    )

    row_y = next_y
    row_x = 0
    row_height = 0
    for panel in missing_panels:
        panel["id"] = next_id
        next_id += 1
        grid = panel.setdefault("gridPos", {})
        width = max(1, min(int(grid.get("w", 12)), 24))
        height = max(1, int(grid.get("h", 8)))
        if row_x and row_x + width > 24:
            row_y += row_height
            row_x = 0
            row_height = 0
        grid.update({"x": row_x, "y": row_y, "w": width, "h": height})
        row_x += width
        row_height = max(row_height, height)

    return existing_panels + missing_panels, len(missing_panels)


def save_generated_dashboard(client, dashboard_response, panels, message):
    dashboard = dashboard_response["dashboard"]
    meta = dashboard_response.get("meta", {})
    dashboard["panels"] = panels
    dashboard["version"] = dashboard.get("version", 0) + 1
    payload = {
        "dashboard": dashboard,
        "overwrite": True,
        "message": message,
    }
    if meta.get("folderUid"):
        payload["folderUid"] = meta["folderUid"]
    client.save_dashboard(payload)


if __name__ == "__main__":
    main()

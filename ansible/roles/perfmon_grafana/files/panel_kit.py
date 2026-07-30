"""Datasource-agnostic Grafana panel builders, shared across dashboard lines.

A line binds its own datasource and target() once: kit = PanelKit(DS, target).
Panel ids come from the kit instance, so each line has its own counter.
"""


class PanelKit:
    """Panel builders bound to one datasource and one target() implementation."""

    def __init__(self, ds: dict, target):
        self._ds = ds
        self._target = target
        self._id = 0

    def nid(self) -> int:
        """Allocate the next panel id."""
        self._id += 1
        return self._id

    def reset_id(self) -> None:
        """Reset the panel id counter to 0 before building a new dashboard."""
        self._id = 0

    @staticmethod
    def thresholds(*steps: tuple[str, float | None]) -> dict:
        """steps: (color, value) pairs; first value should be None."""
        return {
            "mode": "absolute",
            "steps": [{"color": c, "value": v} for c, v in steps],
        }

    def timeseries(
        self,
        title: str,
        x: int,
        y: int,
        w: int,
        h: int,
        targets: list[dict],
        unit: str = "short",
        stacked: bool = False,
        bars: bool = False,
        max_: float | None = None,
        fill: int = 12,
        axis_label: str | None = None,
        description: str | None = None,
    ) -> dict:
        """Build a timeseries panel."""
        custom = {
            "drawStyle": "bars" if bars else "line",
            "lineInterpolation": "smooth",
            "lineWidth": 1,
            "fillOpacity": 80 if bars else fill,
            "showPoints": "never",
            "spanNulls": True,
            "stacking": {"mode": "normal" if stacked else "none", "group": "A"},
        }
        if axis_label:
            custom["axisLabel"] = axis_label
        defaults = {
            "color": {"mode": "palette-classic"},
            "custom": custom,
            "unit": unit,
        }
        if max_ is not None:
            defaults["max"] = max_
            defaults["min"] = 0
        panel = {
            "id": self.nid(),
            "type": "timeseries",
            "title": title,
            "datasource": self._ds,
            "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {
                "legend": {
                    "displayMode": "list",
                    "placement": "bottom",
                    "showLegend": True,
                    "calcs": [],
                },
                "tooltip": {"mode": "multi", "sort": "desc"},
            },
            "targets": targets,
        }
        if description:
            panel["description"] = description
        return panel

    def state_timeline(
        self,
        title: str,
        x: int,
        y: int,
        w: int,
        h: int,
        sql: str,
        states: list[tuple[int, str, str]],
        description: str | None = None,
    ) -> dict:
        """Build a state-timeline panel: one colored band per state run, per series.

        states are (stored value, display text, color). The query returns time / series
        name / numeric state, and value mappings turn each level into a labelled band -
        a string value column would not survive the time_series frame conversion.
        """
        panel = {
            "id": self.nid(),
            "type": "state-timeline",
            "title": title,
            "datasource": self._ds,
            "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "fieldConfig": {
                "defaults": {
                    "color": {"mode": "thresholds"},
                    "custom": {"fillOpacity": 90, "lineWidth": 0},
                    "mappings": [
                        {
                            "type": "value",
                            "options": {
                                str(value): {"text": text, "color": color, "index": i}
                                for i, (value, text, color) in enumerate(states)
                            },
                        }
                    ],
                    "thresholds": self.thresholds(("text", None)),
                },
                "overrides": [],
            },
            "options": {
                "showValue": "auto",
                "mergeValues": True,
                "alignValue": "center",
                "rowHeight": 0.9,
                "legend": {"displayMode": "list", "placement": "bottom"},
                "tooltip": {"mode": "single", "sort": "none"},
            },
            "targets": [self._target(sql)],
        }
        if description:
            panel["description"] = description
        return panel

    def text_panel(self, title, x, y, w, h, content):
        """Build a markdown text panel."""
        return {
            "id": self.nid(),
            "type": "text",
            "title": title,
            "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "options": {"mode": "markdown", "content": content},
        }

    def stat(
        self,
        title: str,
        x: int,
        y: int,
        w: int,
        h: int,
        sql: str,
        unit: str,
        th: dict,
        links: list[dict] | None = None,
        decimals: int = 0,
        mappings: dict | None = None,
        overrides: list[dict] | None = None,
        show_values: bool = False,
        fields: str = "",
    ) -> dict:
        """Build a single-value stat panel."""
        p = {
            "id": self.nid(),
            "type": "stat",
            "title": title,
            "datasource": self._ds,
            "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "fieldConfig": {
                "defaults": {
                    "color": {"mode": "thresholds"},
                    "decimals": decimals,
                    "thresholds": th,
                    "unit": unit,
                },
                "overrides": overrides or [],
            },
            "options": {
                "reduceOptions": {
                    "calcs": ["lastNotNull"],
                    "fields": fields,
                    "values": show_values,
                },
                "colorMode": "background",
                "graphMode": "none",
                "justifyMode": "auto",
                "orientation": "auto",
                "textMode": "auto",
            },
            "targets": [self._target(sql, "table")],
        }
        if mappings:
            p["fieldConfig"]["defaults"]["mappings"] = [
                {
                    "type": "value",
                    "options": {
                        k: {"color": c, "index": i}
                        for i, (k, c) in enumerate(mappings.items())
                    },
                }
            ]
        if links:
            p["links"] = links
        return p

    def table(
        self,
        title: str,
        x: int,
        y: int,
        w: int,
        h: int,
        sql: str,
        overrides: list[dict] | None = None,
        sort_by: list[dict] | None = None,
        description: str | None = None,
    ) -> dict:
        """Build a table panel."""
        panel = {
            "id": self.nid(),
            "type": "table",
            "title": title,
            "datasource": self._ds,
            "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "fieldConfig": {
                "defaults": {
                    "custom": {
                        "align": "auto",
                        "cellOptions": {"type": "auto"},
                        "filterable": True,
                    },
                    "thresholds": self.thresholds(("green", None)),
                },
                "overrides": overrides or [],
            },
            "options": {
                "showHeader": True,
                "cellHeight": "sm",
                "footer": {"show": False, "reducer": ["sum"], "fields": ""},
                "sortBy": sort_by or [],
            },
            "targets": [self._target(sql, "table")],
        }
        if description:
            panel["description"] = description
        return panel

    def bargauge(
        self, title: str, x: int, y: int, w: int, h: int, sql: str, unit: str = "s"
    ) -> dict:
        """Build a horizontal bar gauge panel."""
        return {
            "id": self.nid(),
            "type": "bargauge",
            "title": title,
            "datasource": self._ds,
            "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "fieldConfig": {
                "defaults": {
                    "color": {"mode": "continuous-GrYlRd"},
                    "thresholds": self.thresholds(("green", None)),
                    "unit": unit,
                },
                "overrides": [],
            },
            "options": {
                "displayMode": "gradient",
                "orientation": "horizontal",
                "reduceOptions": {
                    "calcs": ["lastNotNull"],
                    "fields": "/^(?!.*name|.*type).*$/",
                    "values": True,
                },
                "showUnfilled": True,
                "valueMode": "color",
                "namePlacement": "left",
            },
            "targets": [self._target(sql, "table")],
        }

    def row(self, title: str, y: int, repeat: str | None = None) -> dict:
        """Build a collapsible row panel used as a section header."""
        r = {
            "id": self.nid(),
            "type": "row",
            "title": title,
            "collapsed": False,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "panels": [],
        }
        if repeat:
            r["repeat"] = repeat
        return r

    @staticmethod
    def flow(panels, y, items):
        """Lay out (w, h, factory) items in a 24-column grid, wrapping on overflow.
        factory(x, y, w, h) returns a panel dict or a list of them. Returns the y below
        the last line, so callers can chain sections without hand-computed offsets.
        """
        x = 0
        line_h = 0
        for w, h, factory in items:
            if x + w > 24:
                y += line_h
                x = 0
                line_h = 0
            built = factory(x, y, w, h)
            if built is not None:
                panels.extend(built if isinstance(built, list) else [built])
            x += w
            line_h = max(line_h, h)
        return y + line_h

    def stat_grid(self, specs, cols=2):
        """flow() factory placing stat() panels in a cols-wide sub-grid inside its
        envelope, so small stat cards can share a line with a taller chart.
        specs: dicts with title/sql/th and optionally unit."""

        def factory(x, y, w, h):
            rows = -(-len(specs) // cols)
            cell_w, cell_h = w // cols, h // rows
            return [
                self.stat(
                    s["title"],
                    x + (i % cols) * cell_w,
                    y + (i // cols) * cell_h,
                    cell_w,
                    cell_h,
                    s["sql"],
                    s.get("unit", "short"),
                    s["th"],
                )
                for i, s in enumerate(specs)
            ]

        return factory

    def subtab(self, panels, title, y, items):
        """A row() header followed by a flow() grid. Returns the y for the next row()."""
        panels.append(self.row(title, y))
        return self.flow(panels, y + 1, items)

    @staticmethod
    def reflow(panel, appended=False):
        """flow() factory that positions an already-built panel, for when its id must
        exist before another panel data-links to it. appended=True if it is already in
        panels[]; the factory then only sets gridPos and returns None."""

        def factory(x, y, w, h):
            panel["gridPos"] = {"h": h, "w": w, "x": x, "y": y}
            return None if appended else panel

        return factory


def col_gauge_bar(col, min_val=0, max_val=100, unit="percent"):
    """Table override: render a column as an inline bar gauge cell.

    max_val=None omits the ceiling, so each bar scales against its own column's largest value.
    """
    properties = [{"id": "min", "value": min_val}]
    if max_val is not None:
        properties.append({"id": "max", "value": max_val})
    properties += [
        {"id": "unit", "value": unit},
        {"id": "color", "value": {"mode": "fixed", "fixedColor": "blue"}},
        {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "basic"}},
    ]
    return {"matcher": {"id": "byName", "options": col}, "properties": properties}


def status_colors(col, mapping):
    """Table override: colored background cell driven by value mappings."""
    return {
        "matcher": {"id": "byName", "options": col},
        "properties": [
            {
                "id": "mappings",
                "value": [
                    {
                        "type": "value",
                        "options": {
                            k: {"color": c, "index": i}
                            for i, (k, c) in enumerate(mapping.items())
                        },
                    }
                ],
            },
            {"id": "custom.cellOptions", "value": {"type": "color-background"}},
        ],
    }


def col_unit(col, unit, display_name=None):
    """Table override: set the display unit (and optionally label) of a column."""
    properties = [{"id": "unit", "value": unit}]
    if display_name:
        properties.append({"id": "displayName", "value": display_name})
    return {"matcher": {"id": "byName", "options": col}, "properties": properties}


def col_thresholds(col, *steps):
    """Table override: threshold-colored background on a numeric column.

    steps: (color, value) pairs; first value should be None.
    """
    return {
        "matcher": {"id": "byName", "options": col},
        "properties": [
            {"id": "thresholds", "value": PanelKit.thresholds(*steps)},
            {"id": "color", "value": {"mode": "thresholds"}},
            {"id": "custom.cellOptions", "value": {"type": "color-background"}},
        ],
    }


def col_hidden(col):
    """Table override hiding a column that only exists to feed a data link."""
    return {
        "matcher": {"id": "byName", "options": col},
        "properties": [{"id": "custom.hidden", "value": True}],
    }


def col_datalink(col, title, url):
    """Table field override that attaches a data link to a single column."""
    return {
        "matcher": {"id": "byName", "options": col},
        "properties": [
            {
                "id": "links",
                "value": [{"title": title, "url": url, "targetBlank": False}],
            }
        ],
    }


def col_datalinks(col, links):
    """Table field override that attaches multiple data links to a single column.
    links is a list of (title, url) tuples; Grafana renders them as a menu on click."""
    return {
        "matcher": {"id": "byName", "options": col},
        "properties": [
            {
                "id": "links",
                "value": [
                    {"title": title, "url": url, "targetBlank": False}
                    for title, url in links
                ],
            }
        ],
    }


def text_var(name, label, default):
    """Grafana textbox variable, populated via URL parameter by data links.

    Use a non-empty sentinel ("*") for an optional filter: ${var:sqlstring} collapses to a
    zero-length token on a cold load when the value is "".
    """
    return {
        "name": name,
        "label": label,
        "type": "textbox",
        "current": {"text": default, "value": default},
        "options": [{"text": default, "value": default}] if default else [],
        "hide": 0,
    }

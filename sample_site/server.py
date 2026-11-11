"""示例公共采购公告站（仅依赖 Python 标准库）。

站内提供两个采购目录（两个列表入口），用于演示「同编号公告按列表入口
分别保存、更正/撤销只沿本入口编号查找引用」的修复：

  /announcements    目录一（综合采购目录），详情前缀 /notice/{编号}
  /announcements-2  目录二（专项采购目录），详情前缀 /notice2/{编号}

目录一场景（链 A~H 与旧版一致，另加双目录同号场景）：
  链 A：首次 + 更正（含截止时间继承）
  链 B：首次 + 更正 + 撤销（撤销使链失效）
  链 C：并行更正（同一公告有两个后继 -> 冲突）
  链 D：引用编号不存在（missing_reference -> 冲突 + 孤立）
  链 E：引用成环（reference_cycle -> 孤立）
  链 F：更正无引用编号（missing_reference -> 孤立）
  链 G：同一项目两个独立首次公告（保持两条独立链）
  公告 H：可通过 /fail-toggle 控制详情页返回 500，验证失败保留上次结果
  X-001 / X-002：目录一自己的同号公告与更正（X-002 只引用目录一的 X-001）
  X-009：更正引用 X-010；X-010 只存在于目录二，目录一应判 missing_reference，
         绝不跨目录借用目录二的 X-010

目录二场景：
  X-001 / X-002：与目录一同编号但内容不同（截止时间不同），独立成链、互不覆盖
  Y-001：目录二独立首次公告
  X-010：被目录一 X-009 引用，但只服务于目录二内部
  Z-001：可切换 500 故障，验证一个入口的详情失败不改动另一入口的同号公告

控制接口：
  GET /fail-toggle?enabled=1|0[&catalog=/announcements-2][&no=Z-001]
  GET /fail-status
"""
from __future__ import annotations

import os
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# 每条公告：编号、项目编号、类型、发布时间、截止时间、引用编号
CATALOG_1: list[dict] = [
    # 链 A：正常链，COR-A-02 不带截止时间（应继承首次的截止时间）
    {"no": "A-001", "project": "PRJ-A", "type": "首次公告",
     "publish": "2026-10-20 09:00:00", "deadline": "2026-11-20 09:30:00",
     "ref": ""},
    {"no": "A-002", "project": "PRJ-A", "type": "更正公告",
     "publish": "2026-10-25 14:00:00", "deadline": "",
     "ref": "A-001"},
    {"no": "A-003", "project": "PRJ-A", "type": "更正公告",
     "publish": "2026-11-01 10:00:00", "deadline": "2026-11-25 15:00:00",
     "ref": "A-002"},

    # 链 B：撤销使整条链失效
    {"no": "B-001", "project": "PRJ-B", "type": "首次公告",
     "publish": "2026-10-21 09:00:00", "deadline": "2026-11-21 09:30:00",
     "ref": ""},
    {"no": "B-002", "project": "PRJ-B", "type": "更正公告",
     "publish": "2026-10-26 14:00:00", "deadline": "2026-11-22 09:30:00",
     "ref": "B-001"},
    {"no": "B-003", "project": "PRJ-B", "type": "撤销公告",
     "publish": "2026-10-28 16:00:00", "deadline": "",
     "ref": "B-002"},

    # 链 C：并行更正（C-002 与 C-003 同时引用 C-001）
    {"no": "C-001", "project": "PRJ-C", "type": "首次公告",
     "publish": "2026-10-22 09:00:00", "deadline": "2026-11-22 09:30:00",
     "ref": ""},
    {"no": "C-002", "project": "PRJ-C", "type": "更正公告",
     "publish": "2026-10-27 10:00:00", "deadline": "2026-11-23 09:30:00",
     "ref": "C-001"},
    {"no": "C-003", "project": "PRJ-C", "type": "更正公告",
     "publish": "2026-10-27 15:00:00", "deadline": "2026-11-24 09:30:00",
     "ref": "C-001"},

    # 链 D：引用了不存在的公告 X-999
    {"no": "D-001", "project": "PRJ-D", "type": "更正公告",
     "publish": "2026-10-23 09:00:00", "deadline": "2026-11-23 09:30:00",
     "ref": "X-999"},

    # 链 E：成环 E-001 -> E-002 -> E-001
    {"no": "E-001", "project": "PRJ-E", "type": "更正公告",
     "publish": "2026-10-24 09:00:00", "deadline": "2026-11-24 09:30:00",
     "ref": "E-002"},
    {"no": "E-002", "project": "PRJ-E", "type": "更正公告",
     "publish": "2026-10-24 10:00:00", "deadline": "2026-11-25 09:30:00",
     "ref": "E-001"},

    # 链 F：更正公告没有引用编号
    {"no": "F-001", "project": "PRJ-F", "type": "更正公告",
     "publish": "2026-10-25 09:00:00", "deadline": "2026-11-26 09:30:00",
     "ref": ""},

    # 链 G：同一项目两个独立首次公告
    {"no": "G-001", "project": "PRJ-G", "type": "首次公告",
     "publish": "2026-10-26 09:00:00", "deadline": "2026-11-26 09:30:00",
     "ref": ""},
    {"no": "G-101", "project": "PRJ-G", "type": "首次公告",
     "publish": "2026-10-27 09:00:00", "deadline": "2026-11-27 09:30:00",
     "ref": ""},
    {"no": "G-102", "project": "PRJ-G", "type": "更正公告",
     "publish": "2026-10-29 09:00:00", "deadline": "2026-11-28 09:30:00",
     "ref": "G-101"},

    # H：详情页故障开关目标（默认正常）
    {"no": "H-001", "project": "PRJ-H", "type": "首次公告",
     "publish": "2026-10-30 09:00:00", "deadline": "2026-11-30 09:30:00",
     "ref": ""},

    # 双目录同号场景：目录一自己的 X-001 链（截止时间与目录二不同）
    {"no": "X-001", "project": "PRJ-X1", "type": "首次公告",
     "publish": "2026-11-02 09:00:00", "deadline": "2026-12-05 09:30:00",
     "ref": ""},
    {"no": "X-002", "project": "PRJ-X1", "type": "更正公告",
     "publish": "2026-11-04 10:00:00", "deadline": "2026-12-08 15:00:00",
     "ref": "X-001"},
    # 引用陷阱：X-010 只存在于目录二；目录一必须判 missing_reference，
    # 不能借用目录二的 X-010。
    {"no": "X-009", "project": "PRJ-X1", "type": "更正公告",
     "publish": "2026-11-05 10:00:00", "deadline": "2026-12-09 15:00:00",
     "ref": "X-010"},
]

CATALOG_2: list[dict] = [
    # 与目录一同编号 X-001/X-002，但属于不同采购目录，内容独立
    {"no": "X-001", "project": "PRJ-X2", "type": "首次公告",
     "publish": "2026-11-03 09:00:00", "deadline": "2026-12-15 09:30:00",
     "ref": ""},
    {"no": "X-002", "project": "PRJ-X2", "type": "更正公告",
     "publish": "2026-11-06 14:00:00", "deadline": "2026-12-18 15:00:00",
     "ref": "X-001"},
    # 目录二独立首次公告
    {"no": "Y-001", "project": "PRJ-Y", "type": "首次公告",
     "publish": "2026-11-07 09:00:00", "deadline": "2026-12-20 09:30:00",
     "ref": ""},
    # 目录一 X-009 引用的编号只在本目录存在，禁止跨目录借用
    {"no": "X-010", "project": "PRJ-X2", "type": "首次公告",
     "publish": "2026-11-08 09:00:00", "deadline": "2026-12-21 09:30:00",
     "ref": ""},
    # 故障开关目标：验证本入口失败不影响目录一的同号/其他公告
    {"no": "Z-001", "project": "PRJ-Z", "type": "首次公告",
     "publish": "2026-11-09 09:00:00", "deadline": "2026-12-22 09:30:00",
     "ref": ""},
]

# 列表入口路径 -> （目录标题、详情前缀、公告列表）
CATALOGS: dict[str, dict] = {
    "/announcements": {
        "title": "公共采购公告列表 · 目录一（综合采购目录）",
        "detail_prefix": "/notice",
        "notices": CATALOG_1,
    },
    "/announcements-2": {
        "title": "公共采购公告列表 · 目录二（专项采购目录）",
        "detail_prefix": "/notice2",
        "notices": CATALOG_2,
    },
}

# (列表入口, 公告编号) -> 公告数据
NOTICE_INDEX: dict[tuple[str, str], dict] = {
    (path, n["no"]): n
    for path, catalog in CATALOGS.items()
    for n in catalog["notices"]
}

# 兼容旧名称：目录一的编号索引
NOTICE_BY_NO = {n["no"]: n for n in CATALOG_1}
NOTICES = CATALOG_1


def _fail_key(catalog_path: str, no: str) -> str:
    return f"{catalog_path}|{no}"


# 运行期故障开关（内存态，重启容器即恢复）：key = "列表入口|公告编号"
FAIL_ENABLED: dict[str, bool] = {
    _fail_key("/announcements", "H-001"): False,
    _fail_key("/announcements-2", "Z-001"): False,
}


def reset_failures() -> None:
    """恢复全部详情页故障开关（测试夹具使用）。"""
    for key in FAIL_ENABLED:
        FAIL_ENABLED[key] = False


PAGE_CSS = """
body { font-family: sans-serif; margin: 2em; color: #222; }
h1 { font-size: 1.4em; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #999; padding: 6px 10px; text-align: left; }
th { background: #f0f3f7; }
a { color: #1a56c4; }
.meta { color: #666; margin-bottom: 1em; }
"""


def render_home() -> str:
    items = "".join(
        f'<li><a href="{escape(path)}">{escape(c["title"])}</a>'
        f'（{len(c["notices"])} 条）</li>'
        for path, c in CATALOGS.items()
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>示例公共采购公告站</title><style>{PAGE_CSS}</style></head>
<body>
<h1>示例公共采购公告站</h1>
<p class="meta">两个采购目录存在相同公告编号（X-001 / X-002），用于演示按列表入口隔离。</p>
<ul>{items}</ul>
</body></html>"""


def render_list(catalog_path: str) -> str:
    catalog = CATALOGS[catalog_path]
    prefix = catalog["detail_prefix"]
    rows = "\n".join(
        '<tr><td><a href="{prefix}/{no}">{no}</a></td>'
        '<td>{project}</td><td>{type}</td><td>{publish}</td>'
        '<td>{ref}</td></tr>'.format(
            prefix=prefix,
            no=escape(n["no"]), project=escape(n["project"]),
            type=escape(n["type"]), publish=escape(n["publish"]),
            ref=escape(n["ref"]) or "—",
        )
        for n in catalog["notices"]
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{escape(catalog['title'])}</title><style>{PAGE_CSS}</style></head>
<body>
<h1>{escape(catalog['title'])}</h1>
<p class="meta">共 {len(catalog['notices'])} 条公告 · 仅用于演示采集 API ·
<a href="/">← 返回站点首页</a></p>
<table class="announcement-list">
<thead><tr><th>公告编号</th><th>项目编号</th><th>类型</th>
<th>发布时间</th><th>引用编号</th></tr></thead>
<tbody>{rows}</tbody>
</table>
</body></html>"""


def _row(label: str, value: str) -> str:
    return (
        f"<tr><th>{escape(label)}</th>"
        f"<td>{escape(value) if value else '—'}</td></tr>"
    )


def render_detail(catalog_path: str, no: str) -> str:
    n = NOTICE_INDEX[(catalog_path, no)]
    back = catalog_path
    rows = "".join([
        _row("公告编号", n["no"]),
        _row("项目编号", n["project"]),
        _row("公告类型", n["type"]),
        _row("发布时间", n["publish"]),
        _row("投标截止时间", n["deadline"]),
        _row("引用编号", n["ref"]),
    ])
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>公告 {escape(n['no'])}</title><style>{PAGE_CSS}</style></head>
<body>
<h1>公告详情 {escape(n['no'])}（{escape(catalog_path)}）</h1>
<p><a href="{escape(back)}">← 返回列表</a></p>
<table class="detail-table"><tbody>{rows}</tbody></table>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: str, status: int = 200,
              content_type: str = "text/html; charset=utf-8") -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path == "/":
            self._send(render_home())
            return

        if path in CATALOGS:
            self._send(render_list(path))
            return

        if path == "/fail-toggle":
            enabled = (query.get("enabled", ["0"])[0] in {"1", "true", "yes"})
            catalog_path = query.get("catalog", ["/announcements"])[0]
            no = query.get("no", ["H-001"])[0]
            if catalog_path not in CATALOGS:
                self._send(
                    f'{{"error": "unknown catalog {catalog_path}"}}',
                    status=400,
                    content_type="application/json; charset=utf-8",
                )
                return
            key = _fail_key(catalog_path, no)
            FAIL_ENABLED[key] = enabled
            self._send(
                f'{{"catalog": {catalog_path!r}, "notice_no": {no!r}, '
                f'"fail_enabled": {str(enabled).lower()}}}',
                content_type="application/json; charset=utf-8",
            )
            return

        if path == "/fail-status":
            statuses = {key: bool(on) for key, on in FAIL_ENABLED.items()}
            import json
            self._send(
                json.dumps(statuses, ensure_ascii=False),
                content_type="application/json; charset=utf-8",
            )
            return

        # 详情页：两个目录使用不同前缀
        for catalog_path, catalog in CATALOGS.items():
            prefix = catalog["detail_prefix"] + "/"
            if path.startswith(prefix):
                no = path[len(prefix):]
                if (catalog_path, no) not in NOTICE_INDEX:
                    self._send("<h1>404 公告不存在</h1>", status=404)
                    return
                if FAIL_ENABLED.get(_fail_key(catalog_path, no)):
                    self._send(
                        "<h1>500 服务器内部错误（模拟故障）</h1>", status=500
                    )
                    return
                self._send(render_detail(catalog_path, no))
                return

        self._send("<h1>404</h1>", status=404)

    def log_message(self, fmt: str, *args) -> None:  # 精简日志
        print(f"[sample-site] {self.address_string()} {fmt % args}")


def main() -> None:
    port = int(os.getenv("SAMPLE_PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[sample-site] 示例公告站监听 0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

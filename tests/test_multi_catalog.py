"""双采购目录同编号场景：按列表入口隔离存储、引用与容错。"""
from __future__ import annotations


CATALOG_1 = "/announcements"
CATALOG_2 = "/announcements-2"


def _chains_by_id(payload: dict) -> dict:
    return {c["chain_id"]: c for c in payload["chains"]}


def _sync_both_catalogs(c, site_base_url) -> None:
    for catalog in (CATALOG_1, CATALOG_2):
        resp = c.post(
            "/api/v1/sync", json={"list_url": f"{site_base_url}{catalog}"}
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"


def test_same_notice_no_saved_separately_per_list_entry(client, site_base_url):
    c, _ = client
    _sync_both_catalogs(c, site_base_url)

    list_url_1 = f"{site_base_url}{CATALOG_1}"
    list_url_2 = f"{site_base_url}{CATALOG_2}"

    # 不带 list_url 且编号命中两个入口 -> 409，返回可选入口
    ambiguous = c.get("/api/v1/announcements/X-001")
    assert ambiguous.status_code == 409
    detail = ambiguous.json()["detail"]
    assert set(detail["list_urls"]) == {list_url_1, list_url_2}

    # 两个入口的 X-001 是各自独立的一行，内容互不覆盖
    first = c.get(
        "/api/v1/announcements/X-001", params={"list_url": list_url_1}
    ).json()
    second = c.get(
        "/api/v1/announcements/X-001", params={"list_url": list_url_2}
    ).json()
    assert first["deadline"] == "2026-12-05 09:30:00"
    assert second["deadline"] == "2026-12-15 09:30:00"
    assert first["list_url"] == list_url_1
    assert second["list_url"] == list_url_2
    assert first["id"] != second["id"]


def test_corrections_resolve_only_within_same_list_entry(client, site_base_url):
    c, _ = client
    _sync_both_catalogs(c, site_base_url)

    list_url_1 = f"{site_base_url}{CATALOG_1}"
    list_url_2 = f"{site_base_url}{CATALOG_2}"

    # 目录二按 list_url 过滤后自成独立链：X-001 -> X-002
    scope2 = c.get(
        "/api/v1/chains", params={"list_url": list_url_2}
    ).json()
    chain2 = _chains_by_id(scope2)["X-001"]
    assert chain2["list_url"] == list_url_2
    assert chain2["status"] == "ok"
    assert [a["notice_no"] for a in chain2["announcements"]] == [
        "X-001", "X-002"
    ]
    assert chain2["effective_deadline"] == "2026-12-18 15:00:00"

    # 目录一自己的 X-001 链也独立存在，截止时间取目录一的数据
    scope1 = c.get(
        "/api/v1/chains", params={"list_url": list_url_1}
    ).json()
    chain1 = _chains_by_id(scope1)["X-001"]
    assert chain1["effective_deadline"] == "2026-12-08 15:00:00"

    # X-009 引用 X-010：X-010 只在目录二存在，目录一必须判缺失引用，
    # 绝不借用目录二的 X-010
    orphans1 = {o["notice_no"]: o for o in scope1["orphans"]}
    x009 = orphans1["X-009"]
    assert x009["list_url"] == list_url_1
    assert "missing_reference" in x009["conflict_reasons"]

    # 目录二的 X-010 自身是正常首次公告，未被目录一的引用污染
    x010 = c.get(
        "/api/v1/announcements/X-010", params={"list_url": list_url_2}
    ).json()
    assert x010["chain_status"] == "ok"
    assert x010["conflict_reasons"] == []


def test_chains_filtered_by_list_url_keeps_entries_apart(client, site_base_url):
    c, _ = client
    _sync_both_catalogs(c, site_base_url)

    list_url_1 = f"{site_base_url}{CATALOG_1}"
    list_url_2 = f"{site_base_url}{CATALOG_2}"

    scope1 = c.get("/api/v1/chains", params={"list_url": list_url_1}).json()
    scope2 = c.get("/api/v1/chains", params={"list_url": list_url_2}).json()

    # 目录二只有 4 条链：X-001、Y-001、X-010、Z-001，无孤立异常
    assert sorted(ch["chain_id"] for ch in scope2["chains"]) == [
        "X-001", "X-010", "Y-001", "Z-001"
    ]
    assert scope2["orphans"] == []

    # 两个目录的链条目都带有自己的 list_url
    assert all(ch["list_url"] == list_url_2 for ch in scope2["chains"])
    assert all(ch["list_url"] == list_url_1 for ch in scope1["chains"])

    # 全量查询时，同 chain_id（X-001）在两个入口各出现一次，不合并
    full = c.get("/api/v1/chains").json()
    x_chains = [ch for ch in full["chains"] if ch["chain_id"] == "X-001"]
    assert {ch["list_url"] for ch in x_chains} == {list_url_1, list_url_2}

    # 未知入口过滤返回空结果而不是报错
    empty = c.get(
        "/api/v1/chains", params={"list_url": "http://unknown/never-synced"}
    ).json()
    assert empty == {"chains": [], "orphans": []}


def test_single_notice_unknown_list_url_returns_404(client, site_base_url):
    c, _ = client
    _sync_both_catalogs(c, site_base_url)
    resp = c.get(
        "/api/v1/announcements/X-001",
        params={"list_url": "http://unknown/never-synced"},
    )
    assert resp.status_code == 404


def test_detail_failure_in_one_entry_keeps_other_entry_intact(
    client, site_base_url, set_fail
):
    c, _ = client
    list_url_1 = f"{site_base_url}{CATALOG_1}"
    list_url_2 = f"{site_base_url}{CATALOG_2}"

    # 两个目录先同步成功
    _sync_both_catalogs(c, site_base_url)

    # 打开目录二 Z-001 的详情故障，重新同步目录二
    set_fail(True, catalog=CATALOG_2, no="Z-001")
    partial = c.post(
        "/api/v1/sync", json={"list_url": list_url_2}
    ).json()
    assert partial["status"] == "partial"
    assert partial["failed_count"] == 1
    assert partial["kept_last"] == 1

    # 目录二 Z-001 保留上次完整详情，仅记录抓取错误
    z001 = c.get(
        "/api/v1/announcements/Z-001", params={"list_url": list_url_2}
    ).json()
    assert z001["deadline"] == "2026-12-22 09:30:00"
    assert "500" in z001["last_fetch_error"]

    # 目录一的数据完全未被触碰
    first = c.get(
        "/api/v1/announcements/X-001", params={"list_url": list_url_1}
    ).json()
    assert first["deadline"] == "2026-12-05 09:30:00"
    assert first["last_fetch_error"] is None
    h001 = c.get(
        "/api/v1/announcements/H-001", params={"list_url": list_url_1}
    ).json()
    assert h001["last_fetch_error"] is None

    # 目录二其余公告同样完好
    second = c.get(
        "/api/v1/announcements/X-001", params={"list_url": list_url_2}
    ).json()
    assert second["deadline"] == "2026-12-15 09:30:00"
    assert second["last_fetch_error"] is None


def test_never_synced_detail_failure_creates_scoped_placeholder(
    client, site_base_url, set_fail
):
    c, _ = client
    list_url_2 = f"{site_base_url}{CATALOG_2}"

    # 先制造故障再首次同步目录二：Z-001 从未成功过 -> 占位记录，仅属于目录二
    set_fail(True, catalog=CATALOG_2, no="Z-001")
    result = c.post(
        "/api/v1/sync", json={"list_url": list_url_2}
    ).json()
    assert result["status"] == "partial"
    assert result["kept_last"] == 0
    assert result["failed_count"] == 1

    placeholder = c.get(
        "/api/v1/announcements/Z-001", params={"list_url": list_url_2}
    ).json()
    assert placeholder["notice_type"] is None
    assert placeholder["chain_status"] == "orphan"
    assert "500" in placeholder["last_fetch_error"]

    # 占位记录不会在目录一产生任何同号数据
    assert c.get(
        "/api/v1/announcements/Z-001",
        params={"list_url": f"{site_base_url}{CATALOG_1}"},
    ).status_code == 404


def test_resync_does_not_overwrite_other_list_entry(client, site_base_url):
    c, _ = client
    list_url_1 = f"{site_base_url}{CATALOG_1}"
    list_url_2 = f"{site_base_url}{CATALOG_2}"

    # 先同步目录二，再同步目录一；后一次同步不得覆盖目录二的同号公告
    c.post("/api/v1/sync", json={"list_url": list_url_2})
    c.post("/api/v1/sync", json={"list_url": list_url_1})

    second = c.get(
        "/api/v1/announcements/X-001", params={"list_url": list_url_2}
    ).json()
    first = c.get(
        "/api/v1/announcements/X-001", params={"list_url": list_url_1}
    ).json()
    assert second["project_no"] == "PRJ-X2"
    assert first["project_no"] == "PRJ-X1"
    assert second["deadline"] != first["deadline"]

    # 再次同步目录二，目录一同样保持不变
    c.post("/api/v1/sync", json={"list_url": list_url_2})
    first_again = c.get(
        "/api/v1/announcements/X-001", params={"list_url": list_url_1}
    ).json()
    assert first_again["project_no"] == "PRJ-X1"
    assert first_again["deadline"] == "2026-12-05 09:30:00"

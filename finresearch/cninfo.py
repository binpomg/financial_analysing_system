"""巨潮公开公告目录适配；字段和POST入口经真实小批量请求核验。"""
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urljoin, urlsplit

from .sources import SourceError, _PinnedHTTPSConnection, _stream, _validate_url

ENDPOINT = "https://www.cninfo.com.cn/new/hisAnnouncement/query"


def announcements(query, start_date, end_date, page_size=10, max_pages=1):
    try:
        start, end = datetime.strptime(start_date, "%Y-%m-%d"), datetime.strptime(end_date, "%Y-%m-%d")
    except (ValueError, TypeError):
        raise ValueError("日期必须采用 YYYY-MM-DD 格式")
    if (start.strftime("%Y-%m-%d") != start_date or end.strftime("%Y-%m-%d") != end_date or start > end
            or isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 30
            or isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 20):
        raise ValueError("日期或有界分页参数不合法")
    result, has_more = [], False
    for page in range(1, max_pages + 1):
        canonical, host, port, addresses = _validate_url(ENDPOINT, ["www.cninfo.com.cn"])
        connection = _PinnedHTTPSConnection(host, port, addresses[0], 30)
        try:
            form = urlencode({"pageNum": page, "pageSize": page_size, "column": "szse", "tabName": "fulltext",
                              "searchkey": query, "isHLtitle": "false", "seDate": start_date + "~" + end_date}).encode("utf-8")
            connection.request("POST", urlsplit(canonical).path, body=form,
                               headers={"User-Agent": "FinancialResearchSystem/0.1", "Content-Type": "application/x-www-form-urlencoded", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status != 200:
                raise SourceError("巨潮目录返回 HTTP " + str(response.status))
            payload = json.loads(b"".join(_stream(response, 5 * 1024 * 1024)).decode("utf-8"))
        finally:
            connection.close()
        rows = payload.get("announcements")
        if rows is None and payload.get("totalAnnouncement") == 0:
            rows = []
        if not isinstance(rows, list):
            raise SourceError("巨潮目录格式变化，未将异常响应当成空目录")
        for row in rows:
            published = datetime.fromtimestamp(int(row["announcementTime"]) / 1000, timezone(timedelta(hours=8)))
            if not start_date <= published.strftime("%Y-%m-%d") <= end_date:
                continue
            url = urljoin("https://static.cninfo.com.cn/", row["adjunctUrl"])
            if urlsplit(url).hostname != "static.cninfo.com.cn":
                raise SourceError("公告附件指向非预期主机")
            result.append({"url": url, "title": re.sub("<[^>]+>", "", row["announcementTitle"]),
                           "published_at": published.isoformat(), "stock_code": row["secCode"],
                           "company": row["secName"], "announcement_id": str(row["announcementId"]),
                           "document_type": row.get("adjunctType", "PDF")})
        total = payload.get("totalAnnouncement")
        has_more = bool(payload.get("hasMore")) or (isinstance(total, int) and total > page * page_size)
        if not has_more:
            break
    unique = {item["url"]: item for item in result}
    return {"source": ENDPOINT, "query": query, "start_date": start_date, "end_date": end_date,
            "items": list(unique.values()), "has_more": has_more, "pages_requested": page,
            "scope_note": "按明确来源查询范围取得的有限批次；has_more为true表示尚有未取得目录，不代表已穷尽资料"}

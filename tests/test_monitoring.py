"""Requesting one book must make Chaptarr want that book — in that format —
and nothing else by the author. Asserts the exact payloads Stackarr sends."""
from fakes import SAME_ORIGIN, Resp, book
from stackarr import chaptarr, db

BALDWIN = {"authorName": "James Baldwin", "foreignAuthorId": "hc:1", "folder": "James Baldwin"}
BOOKS = [book(11, "Giovanni's Room", "audiobook"), book(12, "Giovanni's Room", "ebook"),
         book(13, "Go Tell It on the Mountain", "audiobook"),
         book(14, "Go Tell It on the Mountain", "ebook")]


def _monitor_calls(fake):
    return [c[3] for c in fake.calls_to("PUT", "/api/v1/book/monitor")]


def _commands(fake):
    return [c[3] for c in fake.calls_to("POST", "/api/v1/command")]


def test_new_author_is_added_without_monitoring_its_bibliography(fake_chaptarr):
    fake_chaptarr.lookup = [dict(BALDWIN)]
    fake_chaptarr.books_on_add = BOOKS

    res = chaptarr.add_and_search("Giovanni's Room", "James Baldwin", fmt="audiobook")

    assert res["ok"], res
    [(_, _, _, sent)] = fake_chaptarr.calls_to("POST", "/api/v1/author")
    assert sent["audiobookMonitorExisting"] == chaptarr.MONITOR_EXISTING_SELECTED
    assert sent["ebookMonitorExisting"] == chaptarr.MONITOR_EXISTING_NONE
    assert sent["audiobookMonitored"] is True and sent["ebookMonitored"] is False
    assert sent["audiobookMonitorFuture"] is False and sent["ebookMonitorFuture"] is False
    assert sent["audiobookMonitorNewItems"] == sent["ebookMonitorNewItems"] == "none"
    assert sent["monitorNewItems"] == "none" and sent["monitored"] is True
    assert sent["addOptions"] == {"monitor": "none", "booksToMonitor": [],
                                  "searchForMissingBooks": False}
    assert sent["audiobookRootFolderPath"] == "/audiobooks"
    # only the requested book's audiobook row is monitored + searched
    assert _monitor_calls(fake_chaptarr) == [{"bookIds": [11], "monitored": True}]
    assert _commands(fake_chaptarr) == [{"name": "BookSearch", "bookIds": [11]}]
    # the freshly-added author already has the right per-media setup
    assert not fake_chaptarr.calls_to("PUT", "/api/v1/author/100")


def test_ebook_request_for_audiobook_author_enables_ebooks_selected_only(fake_chaptarr):
    # the user set this author to monitor ALL existing audiobooks themselves
    existing = dict(BALDWIN, id=8, monitored=True,
                    audiobookMonitorExisting=chaptarr.MONITOR_EXISTING_ALL,
                    audiobookRootFolderPath="/audiobooks",
                    audiobookQualityProfileId=2, audiobookMetadataProfileId=1,
                    ebookMonitorExisting=0, ebookMonitored=False,
                    ebookMonitorNewItems="all")
    fake_chaptarr.lookup = [dict(BALDWIN)]
    fake_chaptarr.authors = [existing]
    fake_chaptarr.books = {8: BOOKS}

    res = chaptarr.add_and_search("Giovanni's Room", "James Baldwin", fmt="ebook")

    assert res["ok"], res
    assert not fake_chaptarr.calls_to("POST", "/api/v1/author")
    [(_, _, _, put)] = fake_chaptarr.calls_to("PUT", "/api/v1/author/8")
    assert put["ebookMonitorExisting"] == chaptarr.MONITOR_EXISTING_SELECTED
    assert put["ebookMonitorNewItems"] == "none"
    assert put["ebookMonitored"] is True
    assert put["ebookRootFolderPath"] == "/books"
    assert put["ebookQualityProfileId"] == 1 and put["ebookMetadataProfileId"] == 2
    # the user's own audiobook choice is untouched
    assert put["audiobookMonitorExisting"] == chaptarr.MONITOR_EXISTING_ALL
    assert _monitor_calls(fake_chaptarr) == [{"bookIds": [12], "monitored": True}]
    assert _commands(fake_chaptarr) == [{"name": "BookSearch", "bookIds": [12]}]
    # the ebook row is picked from mediaType, without an interactive search
    assert not fake_chaptarr.gets("/api/v1/release")


def test_sibling_lookup_still_used_when_media_is_unknown(fake_chaptarr):
    # a build without mediaType on its rows: fall back to the release view's
    # sibling id, as before
    rows = [{k: v for k, v in b.items() if k != "mediaType"} for b in BOOKS]
    existing = dict(BALDWIN, id=8, monitored=True, ebookMonitorExisting=2,
                    ebookRootFolderPath="/books", ebookQualityProfileId=1,
                    ebookMetadataProfileId=2)
    fake_chaptarr.lookup = [dict(BALDWIN)]
    fake_chaptarr.authors = [existing]
    fake_chaptarr.books = {8: rows}
    real_route = fake_chaptarr._route

    def route(method, url, params=None, **kw):
        if url.endswith("/api/v1/release"):
            fake_chaptarr.calls.append((method, "/api/v1/release", dict(params), None))
            return Resp(200, {"siblingBookId": 12, "siblingMediaType": "ebook"})
        return real_route(method, url, params=params, **kw)
    fake_chaptarr._route = route

    assert chaptarr.add_and_search("Giovanni's Room", "James Baldwin", fmt="ebook")["ok"]
    assert len(fake_chaptarr.gets("/api/v1/release")) == 1
    assert _monitor_calls(fake_chaptarr) == [{"bookIds": [12], "monitored": True}]


def test_author_already_monitored_for_the_media_is_left_alone(fake_chaptarr):
    existing = dict(BALDWIN, id=8, monitored=True,
                    audiobookMonitorExisting=chaptarr.MONITOR_EXISTING_ALL,
                    audiobookMonitorNewItems="all",
                    audiobookRootFolderPath="/audiobooks",
                    audiobookQualityProfileId=2, audiobookMetadataProfileId=1)
    fake_chaptarr.lookup = [dict(BALDWIN)]
    fake_chaptarr.authors = [existing]
    fake_chaptarr.books = {8: BOOKS}

    assert chaptarr.add_and_search("Giovanni's Room", "James Baldwin", fmt="audiobook")["ok"]
    assert not fake_chaptarr.calls_to("PUT", "/api/v1/author/8")
    assert _monitor_calls(fake_chaptarr) == [{"bookIds": [11], "monitored": True}]


def test_whole_author_add_monitors_only_the_requested_media(fake_chaptarr):
    fake_chaptarr.lookup = [dict(BALDWIN)]
    fake_chaptarr.books_on_add = BOOKS

    assert chaptarr.add_and_search("James Baldwin", "James Baldwin", fmt="audiobook")["ok"]
    assert _monitor_calls(fake_chaptarr) == [{"bookIds": [11, 13], "monitored": True}]
    assert _commands(fake_chaptarr) == [{"name": "AuthorSearch", "authorId": 100}]


CRADLE = [book(21, "Unsouled", series="Cradle #1"), book(22, "Soulsmith", series="Cradle #2"),
          book(23, "Unsouled", "ebook", series="Cradle #1"),
          book(24, "House of Blades", series="The Traveler's Gate #1")]
WIGHT = {"authorName": "Will Wight", "foreignAuthorId": "hc:2", "folder": "Will Wight"}


def test_series_add_monitors_just_that_series(fake_chaptarr):
    fake_chaptarr.lookup = [dict(WIGHT)]
    fake_chaptarr.books_on_add = CRADLE

    res = chaptarr.add_and_search("Cradle", "Will Wight", fmt="audiobook", series="Cradle")

    assert res["ok"], res
    assert _monitor_calls(fake_chaptarr) == [{"bookIds": [21, 22], "monitored": True}]
    assert _commands(fake_chaptarr) == [{"name": "BookSearch", "bookIds": [21, 22]}]


def test_series_chaptarr_doesnt_list_falls_back_to_the_author(fake_chaptarr):
    fake_chaptarr.lookup = [dict(WIGHT)]
    fake_chaptarr.books_on_add = [dict(b, seriesTitle="") for b in CRADLE]

    assert chaptarr.add_and_search("Cradle", "Will Wight", fmt="audiobook", series="Cradle")["ok"]
    assert _monitor_calls(fake_chaptarr) == [{"bookIds": [21, 22, 24], "monitored": True}]
    assert _commands(fake_chaptarr) == [{"name": "AuthorSearch", "authorId": 100}]


def _queue(client, title, author, source):
    with db.conn() as c:
        return c.execute("INSERT INTO requests (user_id,title,author,status,source,format) "
                         "VALUES (?,?,?,'pending_approval',?,'audiobook')",
                         (client.uid, title, author, source)).lastrowid


def test_approving_queued_series_and_author_requests(fake_chaptarr, admin_client):
    fake_chaptarr.lookup = [dict(WIGHT)]
    fake_chaptarr.books_on_add = CRADLE

    rid = _queue(admin_client, "Cradle", "Will Wight", "series")
    r = admin_client.post(f"/api/requests/{rid}/approve", headers=SAME_ORIGIN)
    assert r.get_json()["status"] == "handed", r.get_json()
    assert _commands(fake_chaptarr) == [{"name": "BookSearch", "bookIds": [21, 22]}]

    rid = _queue(admin_client, "All books by Will Wight", "Will Wight", "author")
    r = admin_client.post(f"/api/requests/{rid}/approve", headers=SAME_ORIGIN)
    assert r.get_json()["status"] == "handed", r.get_json()
    assert _commands(fake_chaptarr)[-1] == {"name": "AuthorSearch", "authorId": 100}

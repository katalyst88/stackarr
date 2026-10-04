"""chaptarr.mark_read — unmonitor a book the user has already read."""
from fakes import book
from stackarr import chaptarr


def test_unmonitors_under_the_named_author_not_the_top_hit(fake_chaptarr):
    # Chaptarr's fuzzy lookup ranks another author first
    fake_chaptarr.lookup = [{"authorName": "Anna Malaika Tubbs", "foreignAuthorId": "a1"},
                            {"authorName": "James Baldwin", "foreignAuthorId": "a2"}]
    fake_chaptarr.authors = [{"id": 7, "authorName": "Anna Malaika Tubbs", "foreignAuthorId": "a1"},
                             {"id": 8, "authorName": "James Baldwin", "foreignAuthorId": "a2"}]
    fake_chaptarr.books = {7: [book(70, "The Three Mothers", monitored=True)],
                           8: [book(80, "Giovanni's Room", monitored=True)]}

    assert chaptarr.mark_read("Giovanni's Room", "James Baldwin") is True
    puts = fake_chaptarr.calls_to("PUT", "/api/v1/book/monitor")
    assert [c[3] for c in puts] == [{"bookIds": [80], "monitored": False}]

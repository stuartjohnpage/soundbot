from soundbot.pagination import paginate


class TestPaginate:
    def test_splits_into_correct_pages(self):
        items = list(range(1, 8))  # 7 items
        pages = paginate(items, per_page=3)
        assert len(pages) == 3
        assert pages[0] == [1, 2, 3]
        assert pages[1] == [4, 5, 6]
        assert pages[2] == [7]

    def test_empty_list_returns_empty(self):
        pages = paginate([], per_page=5)
        assert pages == []

    def test_exact_fit(self):
        items = list(range(1, 11))  # 10 items
        pages = paginate(items, per_page=5)
        assert len(pages) == 2
        assert pages[0] == [1, 2, 3, 4, 5]
        assert pages[1] == [6, 7, 8, 9, 10]

    def test_single_page(self):
        items = [1, 2, 3]
        pages = paginate(items, per_page=10)
        assert len(pages) == 1
        assert pages[0] == [1, 2, 3]

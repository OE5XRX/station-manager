from rest_framework.pagination import PageNumberPagination


class StandardResultsSetPagination(PageNumberPagination):
    """Default paginator for the user/automation read API."""

    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200

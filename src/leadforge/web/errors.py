class FetchError(Exception):
    """Base class for errors raised by the web layer."""


class DeniedHost(FetchError):
    pass


class DomainPaused(FetchError):
    pass


class BudgetExhausted(FetchError):
    pass


class RobotsDisallowed(FetchError):
    pass


class Blocked(FetchError):
    pass


class FetchFailed(FetchError):
    pass


class TierUnavailable(FetchError):
    pass

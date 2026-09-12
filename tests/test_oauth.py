"""The OAuth authorization server: the Google round trip, the allow list, tokens."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from queenscoach.config import QUEENSCOACH_SCOPE, GoogleOAuthConfig
from queenscoach.oauth import (
    GoogleAuthError,
    GoogleAuthorizationServerProvider,
    GoogleIdentity,
)

RESOURCE_URL = "https://queenscoach.test/mcp"
CALLBACK_URL = "https://queenscoach.test/auth/google/callback"
CLIENT_REDIRECT = "http://127.0.0.1:33418/callback"


def google_config(**overrides: object) -> GoogleOAuthConfig:
    settings: dict[str, object] = {
        "client_id": "google-client-id",
        "client_secret": "google-client-secret",
        "allowed_emails": frozenset({"rider@example.com"}),
        "allowed_domains": frozenset(),
        "allow_any_account": False,
        "authorization_url": "https://accounts.google.test/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.test/token",
        "jwks_url": "https://www.googleapis.test/oauth2/v3/certs",
        "access_token_ttl_seconds": 3600.0,
        "refresh_token_ttl_seconds": 86400.0,
    }
    settings.update(overrides)
    return GoogleOAuthConfig(**settings)  # type: ignore[arg-type]


class StubResolver:
    """Stands in for Google's token endpoint."""

    def __init__(self, identity: GoogleIdentity | Exception) -> None:
        self._identity = identity
        self.calls: list[dict[str, str]] = []

    async def resolve(self, *, code: str, redirect_uri: str, code_verifier: str) -> GoogleIdentity:
        self.calls.append(
            {"code": code, "redirect_uri": redirect_uri, "code_verifier": code_verifier}
        )
        if isinstance(self._identity, Exception):
            raise self._identity
        return self._identity


ALLOWED = GoogleIdentity(subject="google-sub-1", email="rider@example.com", email_verified=True)


class Clock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def make_provider(
    *,
    identity: GoogleIdentity | Exception = ALLOWED,
    config: GoogleOAuthConfig | None = None,
    clock: Clock | None = None,
) -> tuple[GoogleAuthorizationServerProvider, StubResolver]:
    resolver = StubResolver(identity)
    provider = GoogleAuthorizationServerProvider(
        config or google_config(),
        callback_url=CALLBACK_URL,
        resource_url=RESOURCE_URL,
        resolver=resolver,
        now=clock or Clock(),
    )
    return provider, resolver


def client(client_id: str = "client-1") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="client-secret",  # noqa: S106 (a fixture, not a real credential)
        redirect_uris=[AnyUrl(CLIENT_REDIRECT)],
        scope=QUEENSCOACH_SCOPE,
    )


def params(**overrides: object) -> AuthorizationParams:
    settings: dict[str, object] = {
        "state": "client-state",
        "scopes": [QUEENSCOACH_SCOPE],
        "code_challenge": "client-code-challenge",
        "redirect_uri": AnyUrl(CLIENT_REDIRECT),
        "redirect_uri_provided_explicitly": True,
        "resource": RESOURCE_URL,
    }
    settings.update(overrides)
    return AuthorizationParams(**settings)  # type: ignore[arg-type]


async def sign_in(
    provider: GoogleAuthorizationServerProvider,
    *,
    registered: OAuthClientInformationFull | None = None,
) -> str:
    """Run /authorize plus the Google callback; return where the browser goes next."""
    registered = registered or client()
    await provider.register_client(registered)
    google_url = await provider.authorize(registered, params())
    state = parse_qs(urlsplit(google_url).query)["state"][0]
    return await provider.complete_google_callback(code="google-code", state=state)


def query_of(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


async def test_authorize_sends_the_browser_to_google_with_pkce() -> None:
    provider, _ = make_provider()
    registered = client()
    await provider.register_client(registered)

    url = await provider.authorize(registered, params())

    assert url.startswith("https://accounts.google.test/o/oauth2/v2/auth?")
    query = query_of(url)
    assert query["client_id"] == "google-client-id"
    assert query["redirect_uri"] == CALLBACK_URL
    assert query["response_type"] == "code"
    assert query["code_challenge_method"] == "S256"
    assert query["code_challenge"]
    assert "email" in query["scope"]
    # The state toward Google is this server's own, not the client's.
    assert query["state"] != "client-state"


async def test_a_permitted_account_is_redirected_back_with_an_authorization_code() -> None:
    provider, resolver = make_provider()

    redirect = await sign_in(provider)

    assert redirect.startswith(CLIENT_REDIRECT)
    query = query_of(redirect)
    assert query["state"] == "client-state"
    assert "error" not in query
    # The code toward Google was redeemed against this server's callback URL.
    assert resolver.calls[0]["redirect_uri"] == CALLBACK_URL
    code = await provider.load_authorization_code(client(), query["code"])
    assert code is not None
    assert code.subject == "google-sub-1"
    assert code.resource == RESOURCE_URL
    # PKCE from the MCP client is carried through for the SDK to verify.
    assert code.code_challenge == "client-code-challenge"


@pytest.mark.parametrize(
    "identity",
    [
        GoogleIdentity(subject="s", email="stranger@elsewhere.test", email_verified=True),
        GoogleIdentity(subject="s", email="rider@example.com", email_verified=False),
    ],
    ids=["not-on-the-allow-list", "unverified-address"],
)
async def test_a_denied_account_gets_an_error_and_no_code(identity: GoogleIdentity) -> None:
    provider, _ = make_provider(identity=identity)

    redirect = await sign_in(provider)

    query = query_of(redirect)
    assert query["error"] == "access_denied"
    assert query["state"] == "client-state"
    assert "code" not in query


async def test_a_failed_google_exchange_becomes_an_oauth_error_not_a_crash() -> None:
    provider, _ = make_provider(identity=GoogleAuthError("Google token request failed"))

    redirect = await sign_in(provider)

    assert query_of(redirect)["error"] == "access_denied"


async def test_an_allowed_domain_admits_any_verified_address_on_it() -> None:
    config = google_config(allowed_emails=frozenset(), allowed_domains=frozenset({"example.com"}))
    provider, _ = make_provider(
        identity=GoogleIdentity(subject="s", email="Someone@Example.com", email_verified=True),
        config=config,
    )

    assert "code" in query_of(await sign_in(provider))


async def test_a_state_is_single_use() -> None:
    provider, _ = make_provider()
    registered = client()
    await provider.register_client(registered)
    url = await provider.authorize(registered, params())
    state = query_of(url)["state"]

    await provider.complete_google_callback(code="google-code", state=state)
    with pytest.raises(GoogleAuthError, match="expired or was already used"):
        await provider.complete_google_callback(code="google-code", state=state)


async def test_a_stale_sign_in_is_refused() -> None:
    clock = Clock()
    provider, _ = make_provider(clock=clock)
    registered = client()
    await provider.register_client(registered)
    state = query_of(await provider.authorize(registered, params()))["state"]

    clock.now += 11 * 60
    with pytest.raises(GoogleAuthError):
        await provider.complete_google_callback(code="google-code", state=state)


async def test_a_resource_indicator_for_another_server_is_refused() -> None:
    provider, _ = make_provider()
    registered = client()
    await provider.register_client(registered)

    with pytest.raises(AuthorizeError) as failure:
        await provider.authorize(registered, params(resource="https://elsewhere.test/mcp"))
    assert failure.value.error == "invalid_target"


async def test_a_missing_resource_indicator_still_yields_a_token_for_this_server() -> None:
    provider, _ = make_provider()
    registered = client()
    await provider.register_client(registered)
    url = await provider.authorize(registered, params(resource=None, scopes=None))
    redirect = await provider.complete_google_callback(
        code="google-code", state=query_of(url)["state"]
    )

    code = await provider.load_authorization_code(registered, query_of(redirect)["code"])
    assert code is not None
    token = await provider.exchange_authorization_code(registered, code)

    access = await provider.load_access_token(token.access_token)
    assert access is not None
    assert access.resource == RESOURCE_URL
    assert access.scopes == [QUEENSCOACH_SCOPE]


async def test_an_authorization_code_cannot_be_redeemed_twice() -> None:
    provider, _ = make_provider()
    registered = client()
    redirect = await sign_in(provider, registered=registered)
    code = await provider.load_authorization_code(registered, query_of(redirect)["code"])
    assert code is not None

    await provider.exchange_authorization_code(registered, code)
    with pytest.raises(TokenError) as failure:
        await provider.exchange_authorization_code(registered, code)
    assert failure.value.error == "invalid_grant"
    assert failure.value.error_description == "Authorization code already redeemed"


async def test_another_client_cannot_load_this_clients_code() -> None:
    provider, _ = make_provider()
    registered = client()
    redirect = await sign_in(provider, registered=registered)
    value = query_of(redirect)["code"]

    impostor = client("client-2")
    await provider.register_client(impostor)
    assert await provider.load_authorization_code(impostor, value) is None


async def test_an_access_token_carries_the_signed_in_identity_and_expires() -> None:
    clock = Clock()
    provider, _ = make_provider(clock=clock)
    registered = client()
    redirect = await sign_in(provider, registered=registered)
    code = await provider.load_authorization_code(registered, query_of(redirect)["code"])
    assert code is not None
    token = await provider.exchange_authorization_code(registered, code)

    access = await provider.load_access_token(token.access_token)
    assert access is not None
    assert access.subject == "google-sub-1"
    assert access.client_id == "client-1"
    assert token.expires_in == 3600

    clock.now += 3601
    assert await provider.load_access_token(token.access_token) is None


async def test_refreshing_rotates_both_tokens() -> None:
    provider, _ = make_provider()
    registered = client()
    redirect = await sign_in(provider, registered=registered)
    code = await provider.load_authorization_code(registered, query_of(redirect)["code"])
    assert code is not None
    first = await provider.exchange_authorization_code(registered, code)
    assert first.refresh_token is not None

    stored = await provider.load_refresh_token(registered, first.refresh_token)
    assert stored is not None
    second = await provider.exchange_refresh_token(registered, stored, [QUEENSCOACH_SCOPE])

    assert second.access_token != first.access_token
    assert second.refresh_token != first.refresh_token
    assert await provider.load_access_token(first.access_token) is None
    assert await provider.load_refresh_token(registered, first.refresh_token) is None
    refreshed = await provider.load_access_token(second.access_token)
    assert refreshed is not None
    assert refreshed.subject == "google-sub-1"


async def test_revoking_an_access_token_also_kills_its_refresh_token() -> None:
    provider, _ = make_provider()
    registered = client()
    redirect = await sign_in(provider, registered=registered)
    code = await provider.load_authorization_code(registered, query_of(redirect)["code"])
    assert code is not None
    token = await provider.exchange_authorization_code(registered, code)
    assert token.refresh_token is not None
    access = await provider.load_access_token(token.access_token)
    assert access is not None

    await provider.revoke_token(access)

    assert await provider.load_access_token(token.access_token) is None
    assert await provider.load_refresh_token(registered, token.refresh_token) is None


async def test_an_unknown_token_is_not_accepted() -> None:
    provider, _ = make_provider()
    assert await provider.load_access_token("not-a-token") is None


async def test_client_registrations_are_bounded_by_evicting_the_oldest() -> None:
    from queenscoach.token_store import MAX_CLIENTS

    provider, _ = make_provider()
    for index in range(MAX_CLIENTS + 1):
        await provider.register_client(client(f"client-{index}"))

    # The newest is still usable and the oldest made room for it.
    assert await provider.get_client(f"client-{MAX_CLIENTS}") is not None
    assert await provider.get_client("client-0") is None

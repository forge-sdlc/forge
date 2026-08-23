"""Tests for the SourceControlProvider protocol boundary."""

from forge.integrations.source_control.contracts import (
    Connection,
    Provider,
    RepositoryRef,
    ResolvedRepository,
    SourceControlProvider,
)


class _CompleteFakeProvider:
    """Implements every SourceControlProvider method (bodies are irrelevant to this test)."""

    async def verify_webhook(self, _headers: object, _body: object) -> bool:
        return True

    async def parse_webhook(self, _headers: object, _body: object, _resolver: object) -> object:
        raise NotImplementedError

    async def resolve_default_branch(self, _repo_ref: object) -> object:
        raise NotImplementedError

    async def get_git_credentials(self, _repo_ref: object) -> object:
        raise NotImplementedError

    async def ensure_write_target(self, _repo_ref: object) -> object:
        raise NotImplementedError

    async def create_change_request(
        self, _repo_ref: object, _target: object, _title: object, _body: object, draft: bool = False
    ) -> object:
        raise NotImplementedError

    async def get_change_request(self, _repo_ref: object, _identity: object) -> object:
        raise NotImplementedError

    async def update_change_request(
        self,
        _repo_ref: object,
        _identity: object,
        *,
        title: object = None,
        body: object = None,
        state: object = None,
    ) -> object:
        raise NotImplementedError

    async def create_comment(self, _repo_ref: object, _identity: object, _body: object) -> object:
        raise NotImplementedError

    async def reply_to_comment(
        self, _repo_ref: object, _identity: object, _comment_id: object, _body: object
    ) -> object:
        raise NotImplementedError

    async def get_review_threads(self, _repo_ref: object, _identity: object) -> object:
        raise NotImplementedError

    async def get_review_thread_comments(self, _repo_ref: object, _identity: object) -> object:
        raise NotImplementedError

    async def get_review_comments_for_submission(
        self, _repo_ref: object, _identity: object, _review_id: object
    ) -> object:
        raise NotImplementedError

    async def get_checks(self, _repo_ref: object, _ref: object) -> object:
        raise NotImplementedError

    async def get_check_logs(self, _repo_ref: object, _check: object) -> object:
        raise NotImplementedError

    async def get_check_artifacts(self, _repo_ref: object, _check: object) -> object:
        raise NotImplementedError

    async def get_file(self, _repo_ref: object, _path: object, _ref: object) -> object:
        raise NotImplementedError

    async def put_file(
        self, _repo_ref: object, _path: object, _content: object, _message: object, _branch: object
    ) -> None:
        raise NotImplementedError

    async def create_branch(self, _repo_ref: object, _name: object, _base: object) -> None:
        raise NotImplementedError

    async def get_authenticated_identity(self, _repo_ref: object) -> object:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class _IncompleteFakeProvider:
    """Missing every method except verify_webhook — must NOT satisfy the protocol."""

    async def verify_webhook(self, _headers: object, _body: object) -> bool:
        return True


def test_complete_fake_satisfies_the_protocol():
    assert isinstance(_CompleteFakeProvider(), SourceControlProvider)


def test_incomplete_fake_does_not_satisfy_the_protocol():
    assert not isinstance(_IncompleteFakeProvider(), SourceControlProvider)


def test_resolved_repository_defaults_to_no_adapter():
    repo_ref = RepositoryRef(
        id="acme/payments",
        provider=Provider.GITHUB,
        connection="github-default",
        namespace="acme/payments",
        default_branch="main",
        change_request_mode="fork",
    )
    connection = Connection(
        name="github-default",
        provider=Provider.GITHUB,
        base_url="https://api.github.com",
        credential_env="GITHUB_TOKEN",
        webhook_secret_env="GITHUB_WEBHOOK_SECRET",
    )

    resolved = ResolvedRepository(repo_ref=repo_ref, connection=connection)

    assert resolved.adapter is None

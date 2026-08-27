"""Unit and integration tests for get-config CLI command."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from forge.cli import cmd_get_config, cmd_project_setup, main


class TestCLIConfigParserAndRouting:
    """Parser and Command Routing Integration Tests."""

    @patch("forge.cli.cmd_get_config", new_callable=AsyncMock)
    @patch("forge.cli.setup_logging")
    def test_routing_get_config(self, _mock_setup_logging, mock_cmd):
        """Calling main(['get-config', 'aisos']) routes to cmd_get_config."""
        mock_cmd.return_value = 0
        code = main(["get-config", "aisos"])
        assert code == 0
        mock_cmd.assert_called_once()
        args = mock_cmd.call_args[0][0]
        assert args.command == "get-config"
        assert args.project_key == "AISOS"  # converts lowercase to uppercase

    @patch("forge.cli.cmd_get_config", new_callable=AsyncMock)
    @patch("forge.cli.setup_logging")
    def test_routing_project_config_alias(self, _mock_setup_logging, mock_cmd):
        """Calling main(['project-config', 'aisos']) successfully maps to cmd_get_config."""
        mock_cmd.return_value = 0
        code = main(["project-config", "aisos"])
        assert code == 0
        mock_cmd.assert_called_once()
        args = mock_cmd.call_args[0][0]
        assert args.command == "get-config"  # set_defaults maps it to get-config
        assert args.project_key == "AISOS"

    @patch("forge.cli.setup_logging")
    def test_mutual_exclusivity(self, _mock_setup_logging):
        """Assert parser raises a parsing error/exits on mutually exclusive options."""
        with pytest.raises(SystemExit):
            main(["get-config", "aisos", "--json", "--property", "forge.repos"])

    @patch("forge.cli.cmd_project_setup", new_callable=AsyncMock)
    @patch("forge.cli.setup_logging")
    def test_project_setup_model_all_parsing(self, _mock_setup_logging, mock_cmd):
        mock_cmd.return_value = 0

        code = main(["project-setup", "aisos", "--model-all", "vertex-prod:gemini-pro"])

        assert code == 0
        args = mock_cmd.call_args.args[0]
        assert args.project_key == "aisos"
        assert args.model_all == "vertex-prod:gemini-pro"

    @patch("forge.cli.cmd_project_setup", new_callable=AsyncMock)
    @patch("forge.cli.setup_logging")
    def test_project_setup_model_removal_parsing(self, _mock_setup_logging, mock_cmd):
        mock_cmd.return_value = 0

        code = main(["project-setup", "aisos", "--remove-model", "generate_prd"])

        assert code == 0
        args = mock_cmd.call_args.args[0]
        assert args.remove_model == ["generate_prd"]

    @patch("forge.cli.cmd_project_setup", new_callable=AsyncMock)
    @patch("forge.cli.setup_logging")
    def test_project_setup_incremental_flags(self, _mock_setup_logging, mock_cmd):
        mock_cmd.return_value = 0

        code = main(
            [
                "project-setup",
                "aisos",
                "--add-repo",
                "org/new",
                "--remove-repo",
                "org/old",
                "--remove-prd-proposals-repo",
                "--remove-prd-proposals-path",
                "--remove-skills",
            ]
        )

        assert code == 0
        args = mock_cmd.call_args.args[0]
        assert args.add_repo == ["org/new"]
        assert args.remove_repo == ["org/old"]
        assert args.remove_prd_proposals_repo is True
        assert args.remove_prd_proposals_path is True
        assert args.remove_skills is True


class TestCLIConfigExecution:
    """Fallback Semantics, Output Serialization, and Discovery."""

    @staticmethod
    def setup_args(**overrides):
        values = {
            "project_key": "PROJ",
            "repo": None,
            "add_repo": None,
            "remove_repo": None,
            "default_repo": None,
            "prd_proposals_repo": None,
            "remove_prd_proposals_repo": False,
            "prd_proposals_path": None,
            "remove_prd_proposals_path": False,
            "skills_config": None,
            "add_skill": None,
            "remove_skills": False,
            "model_policy": None,
            "model": None,
            "model_all": None,
            "remove_model": None,
            "clear_model_policy": False,
            "clear_model_default": False,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    @pytest.mark.asyncio
    async def test_add_and_remove_repos_preserves_other_entries(self):
        jira = MagicMock()
        jira.get_project_property = AsyncMock(
            return_value=["org/keep", "org/remove", {"name": "org/update", "branch": "old"}]
        )
        jira.set_project_property = AsyncMock()
        jira.close = AsyncMock()
        args = self.setup_args(
            add_repo=['{"name":"org/update","branch":"main"}', "org/new"],
            remove_repo=["org/remove"],
        )

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        jira.set_project_property.assert_awaited_once_with(
            "PROJ",
            "forge.repos",
            ["org/keep", {"name": "org/update", "branch": "main"}, "org/new"],
        )

    @pytest.mark.asyncio
    async def test_remove_repo_rejects_empty_result(self, capsys):
        jira = MagicMock()
        jira.get_project_property = AsyncMock(return_value=["org/only"])
        jira.set_project_property = AsyncMock()
        jira.close = AsyncMock()

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(self.setup_args(remove_repo=["org/only"]))

        assert code == 1
        assert "forge.repos cannot be empty" in capsys.readouterr().err
        jira.set_project_property.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_remove_optional_metadata(self):
        jira = MagicMock()
        jira.delete_project_property = AsyncMock()
        jira.close = AsyncMock()
        args = self.setup_args(
            remove_prd_proposals_repo=True,
            remove_prd_proposals_path=True,
            remove_skills=True,
        )

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        assert [item.args for item in jira.delete_project_property.await_args_list] == [
            ("PROJ", "forge.prd_proposals_repo"),
            ("PROJ", "forge.prd_proposals_path"),
            ("PROJ", "forge.skills"),
        ]

    @pytest.mark.asyncio
    async def test_model_flag_preserves_existing_project_overrides(self):
        jira = MagicMock()
        jira.get_project_property = AsyncMock(
            return_value={"generate_prd": {"connection": "vertex", "model": "gemini-pro"}}
        )
        jira.set_project_property = AsyncMock()
        jira.close = AsyncMock()
        args = SimpleNamespace(
            project_key="PROJ",
            repo=None,
            default_repo=None,
            prd_proposals_repo=None,
            prd_proposals_path=None,
            skills_config=None,
            add_skill=None,
            model_policy=None,
            model=["implement_task=vertex:gemini-pro"],
            model_all=None,
        )

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        written = jira.set_project_property.await_args.args[2]
        assert set(written) == {"generate_prd", "implement_task"}

    @pytest.mark.asyncio
    async def test_project_model_override_does_not_require_local_connections(self):
        jira = MagicMock()
        jira.get_project_property = AsyncMock(return_value=None)
        jira.set_project_property = AsyncMock()
        jira.close = AsyncMock()
        args = SimpleNamespace(
            project_key="PROJ",
            repo=None,
            default_repo=None,
            prd_proposals_repo=None,
            prd_proposals_path=None,
            skills_config=None,
            add_skill=None,
            model_policy=None,
            model=["generate_prd=default:gemini-pro"],
            model_all=None,
        )

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        written = jira.set_project_property.await_args.args[2]
        assert written == {"generate_prd": {"connection": "default", "model": "gemini-pro"}}

    @pytest.mark.asyncio
    async def test_remove_model_preserves_other_overrides(self):
        jira = MagicMock()
        jira.get_project_property = AsyncMock(
            return_value={
                "generate_prd": {"connection": "vertex", "model": "gemini-pro"},
                "generate_spec": {"connection": "vertex", "model": "gemini-flash"},
            }
        )
        jira.set_project_property = AsyncMock()
        jira.delete_project_property = AsyncMock()
        jira.close = AsyncMock()
        args = SimpleNamespace(
            project_key="PROJ",
            repo=None,
            default_repo=None,
            prd_proposals_repo=None,
            prd_proposals_path=None,
            skills_config=None,
            add_skill=None,
            model_policy=None,
            model=None,
            model_all=None,
            remove_model=["generate_prd"],
            clear_model_policy=False,
        )

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        assert jira.set_project_property.await_args.args[2] == {
            "generate_spec": {"connection": "vertex", "model": "gemini-flash"}
        }
        jira.delete_project_property.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_remove_last_model_deletes_property(self):
        jira = MagicMock()
        jira.get_project_property = AsyncMock(
            return_value={"generate_prd": {"connection": "vertex", "model": "gemini-pro"}}
        )
        jira.set_project_property = AsyncMock()
        jira.delete_project_property = AsyncMock()
        jira.close = AsyncMock()
        args = SimpleNamespace(
            project_key="PROJ",
            repo=None,
            default_repo=None,
            prd_proposals_repo=None,
            prd_proposals_path=None,
            skills_config=None,
            add_skill=None,
            model_policy=None,
            model=None,
            model_all=None,
            remove_model=["generate_prd"],
            clear_model_policy=False,
        )

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        jira.delete_project_property.assert_awaited_once_with("PROJ", "forge.model_policy")
        jira.set_project_property.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_clear_model_policy_deletes_property(self):
        jira = MagicMock()
        jira.delete_project_property = AsyncMock()
        jira.close = AsyncMock()
        args = SimpleNamespace(
            project_key="PROJ",
            repo=None,
            default_repo=None,
            prd_proposals_repo=None,
            prd_proposals_path=None,
            skills_config=None,
            add_skill=None,
            model_policy=None,
            model=None,
            model_all=None,
            remove_model=None,
            clear_model_policy=True,
        )

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        jira.delete_project_property.assert_awaited_once_with("PROJ", "forge.model_policy")

    @pytest.mark.asyncio
    async def test_model_all_sets_separate_project_default(self):
        jira = MagicMock()
        jira.set_project_property = AsyncMock()
        jira.close = AsyncMock()
        args = SimpleNamespace(
            project_key="PROJ",
            repo=None,
            default_repo=None,
            prd_proposals_repo=None,
            prd_proposals_path=None,
            skills_config=None,
            add_skill=None,
            model_policy=None,
            model=None,
            model_all="vertex:gemini-flash",
            remove_model=None,
            clear_model_policy=False,
            clear_model_default=False,
        )

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        jira.set_project_property.assert_awaited_once_with(
            "PROJ",
            "forge.model_default",
            {"connection": "vertex", "model": "gemini-flash"},
        )
        jira.get_project_property.assert_not_called()

    @pytest.mark.asyncio
    async def test_clear_model_default_deletes_separate_property(self):
        jira = MagicMock()
        jira.delete_project_property = AsyncMock()
        jira.close = AsyncMock()
        args = SimpleNamespace(
            project_key="PROJ",
            repo=None,
            default_repo=None,
            prd_proposals_repo=None,
            prd_proposals_path=None,
            skills_config=None,
            add_skill=None,
            model_policy=None,
            model=None,
            model_all=None,
            remove_model=None,
            clear_model_policy=False,
            clear_model_default=True,
        )

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        jira.delete_project_property.assert_awaited_once_with("PROJ", "forge.model_default")

    @pytest.fixture
    def mock_jira_client(self):
        with patch("forge.integrations.jira.client.JiraClient") as mock:
            client_inst = MagicMock()
            client_inst.list_project_properties = AsyncMock(
                return_value=[
                    "forge.repos",
                    "forge.default_repo",
                    "forge.prd_proposals_repo",
                    "forge.prd_proposals_path",
                    "forge.skills",
                    "forge.references",
                    "forge.model_policy",
                    "forge.model_default",
                ]
            )
            client_inst.get_project_property = AsyncMock(
                side_effect=lambda _pk, key: {
                    "forge.repos": ["org/repo1"],
                    "forge.default_repo": "org/repo1",
                    "forge.prd_proposals_repo": "org/prd",
                    "forge.prd_proposals_path": "/enhancements/",
                    "forge.skills": [{"source": "http://skill"}],
                    "forge.references": None,
                    "forge.model_policy": None,
                    "forge.model_default": {
                        "connection": "vertex",
                        "model": "gemini-flash",
                    },
                }.get(key)
            )
            client_inst.close = AsyncMock()
            mock.return_value = client_inst
            yield client_inst

    @pytest.fixture
    def mock_settings(self):
        from forge.config import Settings

        settings = Settings(
            jira_base_url="https://test.atlassian.net",
            jira_api_token="token",
            jira_user_email="test@example.com",
            github_token="github-token",
            github_known_repos="org/fallback-repo1,org/fallback-repo2",
            github_default_repo="org/fallback-repo1",
            prd_proposals_repo="org/global-prd",
            prd_proposals_path="global-enhancements",
            forge_require_project_config=True,
        )
        with patch("forge.config.get_settings", return_value=settings):
            yield settings

    @pytest.mark.asyncio
    async def test_require_project_config_true(self, mock_jira_client, mock_settings, capsys):
        """Under FORGE_REQUIRE_PROJECT_CONFIG=True, missing props are reported as unset/required."""
        mock_settings.forge_require_project_config = True
        # Set project property for forge.repos to None
        mock_jira_client.get_project_property = AsyncMock(
            side_effect=lambda _pk, key: {
                "forge.repos": None,
                "forge.default_repo": None,
                "forge.prd_proposals_repo": None,
                "forge.prd_proposals_path": None,
                "forge.skills": None,
                "forge.references": None,
            }.get(key)
        )

        class Args:
            project_key = "MYPROJ"
            json = False
            property = None

        code = await cmd_get_config(Args())
        assert code == 0

        out, err = capsys.readouterr()
        # Should NOT inherit fallback settings, except for proposals_path
        assert "forge.repos:" in out and "[required / missing]" in out
        assert "forge.default_repo:" in out and "[required / missing]" in out
        assert "forge.prd_proposals_repo:" in out and "[required / missing]" in out
        assert (
            "forge.prd_proposals_path:" in out
            and "global-enhancements" in out
            and "[default]" in out
        )

    @pytest.mark.asyncio
    async def test_require_project_config_false(self, mock_jira_client, mock_settings, capsys):
        """Under FORGE_REQUIRE_PROJECT_CONFIG=False, missing props fall back to global."""
        mock_settings.forge_require_project_config = False
        mock_jira_client.get_project_property = AsyncMock(
            side_effect=lambda _pk, key: {
                "forge.repos": None,
                "forge.default_repo": None,
                "forge.prd_proposals_repo": None,
                "forge.prd_proposals_path": None,
                "forge.skills": None,
                "forge.references": None,
            }.get(key)
        )

        class Args:
            project_key = "MYPROJ"
            json = False
            property = None

        code = await cmd_get_config(Args())
        assert code == 0

        out, err = capsys.readouterr()
        # Should inherit fallbacks and mark as [default]
        assert "forge.repos:" in out and "org/fallback-repo1" in out and "[default]" in out
        assert "forge.default_repo:" in out and "org/fallback-repo1" in out and "[default]" in out
        assert "forge.prd_proposals_repo:" in out and "org/global-prd" in out and "[default]" in out
        assert (
            "forge.prd_proposals_path:" in out
            and "global-enhancements" in out
            and "[default]" in out
        )

    @pytest.mark.asyncio
    async def test_output_json_mode(self, mock_jira_client, mock_settings, capsys):  # noqa: ARG002
        """JSON output mode conforms to schema."""

        class Args:
            project_key = "MYPROJ"
            json = True
            property = None

        code = await cmd_get_config(Args())
        assert code == 0

        out, err = capsys.readouterr()
        data = json.loads(out)
        assert data["project"] == "MYPROJ"
        assert "project_properties" in data
        assert "global_fallbacks" in data
        assert "effective" in data
        assert (
            data["effective"]["forge.prd_proposals_path"]["value"] == "enhancements"
        )  # stripped slashes
        assert data["effective"]["forge.prd_proposals_path"]["source"] == "project"

    @pytest.mark.asyncio
    async def test_human_output_includes_project_model_default(
        self,
        mock_jira_client,  # noqa: ARG002
        mock_settings,  # noqa: ARG002
        capsys,
    ):
        class Args:
            project_key = "MYPROJ"
            json = False
            property = None

        code = await cmd_get_config(Args())

        assert code == 0
        out, _err = capsys.readouterr()
        assert "forge.model_default:" in out
        assert '"connection": "vertex"' in out
        assert '"model": "gemini-flash"' in out
        assert "[project]" in out

    @pytest.mark.asyncio
    async def test_output_property_queries(self, mock_jira_client, mock_settings, capsys):  # noqa: ARG002
        """--property queries return clean format based on type."""

        class Args:
            project_key = "MYPROJ"
            json = False
            property = "forge.repos"

        # 1. list type
        code = await cmd_get_config(Args())
        assert code == 0
        out, err = capsys.readouterr()
        assert out.strip() == '["org/repo1"]'

        # 2. string type
        Args.property = "forge.default_repo"
        code = await cmd_get_config(Args())
        assert code == 0
        out, err = capsys.readouterr()
        assert out.strip() == "org/repo1"

        # 3. boolean type (mock a boolean property)
        mock_jira_client.list_project_properties = AsyncMock(
            return_value=["forge.repos", "forge.custom_bool"]
        )
        mock_jira_client.get_project_property = AsyncMock(
            side_effect=lambda _pk, key: {
                "forge.repos": ["org/repo1"],
                "forge.custom_bool": True,
            }.get(key)
        )
        Args.property = "forge.custom_bool"
        code = await cmd_get_config(Args())
        assert code == 0
        out, err = capsys.readouterr()
        assert out.strip() == "true"

        # 4. unset/None value prints empty line
        mock_jira_client.list_project_properties = AsyncMock(
            return_value=["forge.repos", "forge.references"]
        )
        mock_jira_client.get_project_property = AsyncMock(
            side_effect=lambda _pk, key: {
                "forge.repos": ["org/repo1"],
                "forge.references": None,
            }.get(key)
        )
        Args.property = "forge.references"
        code = await cmd_get_config(Args())
        assert code == 0
        out, err = capsys.readouterr()
        assert out == "\n"

    @pytest.mark.asyncio
    async def test_dynamic_discovery(self, mock_jira_client, mock_settings, capsys):  # noqa: ARG002
        """Dynamically discovered forge.* properties are listed and resolved."""
        mock_jira_client.list_project_properties = AsyncMock(
            return_value=["forge.repos", "forge.custom_discovered"]
        )
        mock_jira_client.get_project_property = AsyncMock(
            side_effect=lambda _pk, key: {
                "forge.repos": ["org/repo1"],
                "forge.custom_discovered": "custom-val",
            }.get(key)
        )

        class Args:
            project_key = "MYPROJ"
            json = False
            property = None

        code = await cmd_get_config(Args())
        assert code == 0
        out, err = capsys.readouterr()
        assert "forge.custom_discovered:" in out and "custom-val" in out
        assert "forge.custom_discovered:" in out and "custom-val" in out and "[project]" in out


class TestCLIConfigErrorHandling:
    """Robust Error Handling Tests."""

    @pytest.fixture
    def mock_settings(self):
        from forge.config import Settings

        settings = Settings(
            jira_base_url="https://test.atlassian.net",
            jira_api_token="token",
            jira_user_email="test@example.com",
            github_token="github-token",
            forge_require_project_config=True,
        )
        with patch("forge.config.get_settings", return_value=settings):
            yield settings

    @pytest.mark.asyncio
    async def test_unknown_property_via_flag(self, mock_settings, capsys):  # noqa: ARG002
        """Querying an unknown property via --property outputs to sys.stderr and exits with code 1."""
        with patch("forge.integrations.jira.client.JiraClient") as mock:
            client_inst = MagicMock()
            client_inst.list_project_properties = AsyncMock(return_value=["forge.repos"])
            client_inst.get_project_property = AsyncMock(return_value=["org/repo"])
            client_inst.close = AsyncMock()
            mock.return_value = client_inst

            class Args:
                project_key = "MYPROJ"
                json = False
                property = "forge.invalid"

            code = await cmd_get_config(Args())
            assert code == 1

            out, err = capsys.readouterr()
            assert "Error: Unknown property 'forge.invalid'" in err

    @pytest.mark.asyncio
    async def test_jira_connectivity_failure(self, mock_settings, capsys):  # noqa: ARG002
        """Mock Jira connectivity failure, prints to stderr and exits with 1 (no crash)."""
        with patch("forge.integrations.jira.client.JiraClient") as mock:
            client_inst = MagicMock()
            # simulate HTTPStatusError
            req = httpx.Request(
                "GET", "https://test.atlassian.net/rest/api/3/project/MYPROJ/properties"
            )
            resp = httpx.Response(403, request=req)
            client_inst.list_project_properties.side_effect = httpx.HTTPStatusError(
                "Forbidden", request=req, response=resp
            )
            client_inst.close = AsyncMock()
            mock.return_value = client_inst

            class Args:
                project_key = "MYPROJ"
                json = False
                property = None

            code = await cmd_get_config(Args())
            assert code == 1

            out, err = capsys.readouterr()
            assert "Error: Jira API request failed for project 'MYPROJ'" in err

    @pytest.mark.asyncio
    async def test_malformed_properties_payload_graceful_degradation(self, mock_settings, capsys):  # noqa: ARG002
        """Mock malformed payload for forge.repos (string instead of list), resolutions degrades gracefully."""
        with patch("forge.integrations.jira.client.JiraClient") as mock:
            client_inst = MagicMock()
            client_inst.list_project_properties = AsyncMock(return_value=["forge.repos"])
            # Return string value "not-a-list" instead of list
            client_inst.get_project_property = AsyncMock(return_value="not-a-list")
            client_inst.close = AsyncMock()
            mock.return_value = client_inst

            class Args:
                project_key = "MYPROJ"
                json = False
                property = None

            code = await cmd_get_config(Args())
            assert code == 0

            out, err = capsys.readouterr()
            # It prints a warning
            assert "Warning: Project property 'forge.repos' is malformed" in err
            # Under FORGE_REQUIRE_PROJECT_CONFIG=True, degrades to [required / missing]
            assert "forge.repos:" in out and "[required / missing]" in out


class TestCLIReferencesConfig:
    @pytest.mark.asyncio
    async def test_cmd_project_setup_add_references(self, capsys) -> None:
        """Adding references via --add-reference and --ref-description writes correctly to Jira."""
        with patch("forge.integrations.jira.client.JiraClient") as mock_jira_cls:
            mock_jira = MagicMock()
            mock_jira.get_project_references = AsyncMock(return_value=[])
            mock_jira.set_project_references = AsyncMock()
            mock_jira.close = AsyncMock()
            mock_jira_cls.return_value = mock_jira

            class Args:
                project_key = "MYPROJ"
                repo = None
                default_repo = None
                prd_proposals_repo = None
                prd_proposals_path = None
                skills_config = None
                add_skill = None
                remove_skill = None
                list_skills = False
                add_reference = ["https://example.com/ref1", "https://example.com/ref2"]
                ref_description = ["Desc 1", "Desc 2"]
                remove_reference = None
                list_references = True

            code = await cmd_project_setup(Args())
            assert code == 0

            # Verify set_project_references was called with fully normalized URLs
            mock_jira.set_project_references.assert_called_once_with(
                "MYPROJ",
                [
                    {"url": "https://example.com/ref1", "description": "Desc 1"},
                    {"url": "https://example.com/ref2", "description": "Desc 2"},
                ],
            )

            out, err = capsys.readouterr()
            assert "forge.references" in out
            assert "https://example.com/ref1 - Desc 1" in out
            assert "https://example.com/ref2 - Desc 2" in out

    @pytest.mark.asyncio
    async def test_cmd_project_setup_mismatched_description_count(self, capsys) -> None:
        """Mismatched description and reference counts returns code 1 and prints an error."""

        # Scenario 1: description provided, but no add_reference
        class Args1:
            project_key = "MYPROJ"
            repo = None
            default_repo = None
            prd_proposals_repo = None
            prd_proposals_path = None
            skills_config = None
            add_skill = None
            remove_skill = None
            list_skills = False
            add_reference = None
            ref_description = ["Desc 1"]
            remove_reference = None
            list_references = False

        code = await cmd_project_setup(Args1())
        assert code == 1
        out, err = capsys.readouterr()
        assert "Error: --ref-description requires matching number of --add-reference items." in err

        # Scenario 2: mismatched lengths
        class Args2:
            project_key = "MYPROJ"
            repo = None
            default_repo = None
            prd_proposals_repo = None
            prd_proposals_path = None
            skills_config = None
            add_skill = None
            remove_skill = None
            list_skills = False
            add_reference = ["https://example.com/ref1"]
            ref_description = ["Desc 1", "Desc 2"]
            remove_reference = None
            list_references = False

        code = await cmd_project_setup(Args2())
        assert code == 1
        out, err = capsys.readouterr()
        assert "Error: --ref-description requires matching number of --add-reference items." in err

    @pytest.mark.asyncio
    async def test_cmd_project_setup_remove_references(self, capsys) -> None:
        """Removing references via --remove-reference removes them correctly."""
        with patch("forge.integrations.jira.client.JiraClient") as mock_jira_cls:
            mock_jira = MagicMock()
            mock_jira.get_project_references = AsyncMock(
                return_value=[
                    {"url": "https://example.com/ref1", "description": "Desc 1"},
                    {"url": "https://example.com/ref2", "description": "Desc 2"},
                ]
            )
            mock_jira.set_project_references = AsyncMock()
            mock_jira.close = AsyncMock()
            mock_jira_cls.return_value = mock_jira

            class Args:
                project_key = "MYPROJ"
                repo = None
                default_repo = None
                prd_proposals_repo = None
                prd_proposals_path = None
                skills_config = None
                add_skill = None
                remove_skill = None
                list_skills = False
                add_reference = None
                ref_description = None
                remove_reference = ["https://example.com/ref1"]
                list_references = True

            code = await cmd_project_setup(Args())
            assert code == 0

            # Verify set_project_references was called without the removed URL
            mock_jira.set_project_references.assert_called_once_with(
                "MYPROJ",
                [
                    {"url": "https://example.com/ref2", "description": "Desc 2"},
                ],
            )

            out, err = capsys.readouterr()
            assert "https://example.com/ref2 - Desc 2" in out
            assert "https://example.com/ref1" not in out

    @patch("forge.cli.cmd_project_setup", new_callable=AsyncMock)
    @patch("forge.cli.setup_logging")
    def test_cli_parser_registers_references(self, _mock_setup_logging, mock_cmd):
        """Verify argparse parser registers --add-reference, --ref-description, --description, --remove-reference, and --list-references."""
        mock_cmd.return_value = 0
        code = main(
            [
                "project-setup",
                "myproj",
                "--add-reference",
                "https://example.com/ref1",
                "--ref-description",
                "Desc 1",
                "--add-reference",
                "https://example.com/ref2",
                "--description",
                "Desc 2",
                "--remove-reference",
                "https://example.com/ref3",
                "--list-references",
            ]
        )
        assert code == 0
        mock_cmd.assert_called_once()
        args = mock_cmd.call_args[0][0]
        assert args.add_reference == [
            "https://example.com/ref1",
            "https://example.com/ref2",
        ]
        assert args.ref_description == ["Desc 1", "Desc 2"]
        assert args.remove_reference == ["https://example.com/ref3"]
        assert args.list_references is True

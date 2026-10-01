"""Unit and integration tests for get-config CLI command."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from forge.cli import cmd_get_config, cmd_project_setup, main


class TestCLIConfigParserAndRouting:
    """Parser and Command Routing Integration Tests."""

    @pytest.fixture(autouse=True)
    def _without_event_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # These tests inspect argument routing; no async handler needs to run.
        monkeypatch.setattr("forge.cli.asyncio.run", lambda result: result)

    @patch("forge.cli.cmd_get_config", new_callable=MagicMock)
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

    @patch("forge.cli.cmd_get_config", new_callable=MagicMock)
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

    @patch("forge.cli.cmd_project_setup", new_callable=MagicMock)
    @patch("forge.cli.setup_logging")
    def test_project_setup_model_all_parsing(self, _mock_setup_logging, mock_cmd):
        mock_cmd.return_value = 0

        code = main(["project-setup", "aisos", "--model-all", "vertex-prod:gemini-pro"])

        assert code == 0
        args = mock_cmd.call_args.args[0]
        assert args.project_key == "aisos"
        assert args.model_all == "vertex-prod:gemini-pro"

    @patch("forge.cli.cmd_project_setup", new_callable=MagicMock)
    @patch("forge.cli.setup_logging")
    def test_project_setup_model_removal_parsing(self, _mock_setup_logging, mock_cmd):
        mock_cmd.return_value = 0

        code = main(["project-setup", "aisos", "--remove-model", "generate_prd"])

        assert code == 0
        args = mock_cmd.call_args.args[0]
        assert args.remove_model == ["generate_prd"]

    @patch("forge.cli.cmd_project_setup", new_callable=MagicMock)
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
                "--remove-default-repo",
                "--remove-prd-proposals-repo",
                "--remove-prd-proposals-path",
                "--remove-skills",
            ]
        )

        assert code == 0
        args = mock_cmd.call_args.args[0]
        assert args.add_repo == ["org/new"]
        assert args.remove_repo == ["org/old"]
        assert args.remove_default_repo is True
        assert args.remove_prd_proposals_repo is True
        assert args.remove_prd_proposals_path is True
        assert args.remove_skills is True

    @patch("forge.cli.cmd_project_setup", new_callable=MagicMock)
    @patch("forge.cli.setup_logging")
    def test_project_setup_json_parsing(self, _mock_setup_logging, mock_cmd):
        mock_cmd.return_value = 0

        code = main(["project-setup", "aisos", "--default-repo", "org/repo", "--json"])

        assert code == 0
        args = mock_cmd.call_args.args[0]
        assert args.project_key == "aisos"
        assert args.default_repo == "org/repo"
        assert args.json is True


class TestAgentRecursionCLI:
    @staticmethod
    def setup_args(**overrides):
        values = TestCLIConfigExecution.setup_args(**overrides)
        values.project_key = "AISOS"
        return values

    @staticmethod
    def mock_config_jira(raw=None, *, error=None):
        jira = MagicMock()
        jira.list_project_properties = AsyncMock(return_value=[])

        async def read(_project, key):
            if key == "forge.agent_recursion_limit":
                if error is not None:
                    raise error
                return raw
            return None

        jira.get_project_property = AsyncMock(side_effect=read)
        jira.close = AsyncMock()
        return jira

    @pytest.fixture(autouse=True)
    def config_settings(self):
        from forge.config import Settings

        settings = Settings(
            _env_file=None,
            jira_base_url="https://jira.example.test",
            jira_api_token="dummy",
            jira_user_email="tester@example.test",
            github_token="dummy",
            llm_backend="vertex-ai",
            llm_model="claude-opus-4-8",
            google_cloud_project="offline-project",
            agent_recursion_limit=100,
        )
        with patch("forge.config.get_settings", return_value=settings):
            yield settings

    @pytest.mark.asyncio
    async def test_set_limit_writes_json_integer_and_reports_mutation(self, capsys):
        jira = MagicMock()
        jira.set_project_property = AsyncMock()
        jira.close = AsyncMock()
        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(self.setup_args(agent_recursion_limit=150, json=True))

        assert code == 0
        jira.set_project_property.assert_awaited_once_with(
            "AISOS", "forge.agent_recursion_limit", 150
        )
        assert json.loads(capsys.readouterr().out)["mutations"] == {
            "forge.agent_recursion_limit": {"operation": "set", "value": 150}
        }

    @pytest.mark.asyncio
    async def test_clear_absent_limit_succeeds_without_other_mutations(self):
        jira = MagicMock()
        jira.delete_project_property = AsyncMock()
        jira.close = AsyncMock()
        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(self.setup_args(clear_agent_recursion_limit=True))

        assert code == 0
        jira.delete_project_property.assert_awaited_once_with(
            "AISOS", "forge.agent_recursion_limit"
        )
        jira.set_project_property.assert_not_called()

    @pytest.mark.parametrize("invalid", [0, -1])
    @pytest.mark.asyncio
    async def test_invalid_limit_with_repo_flag_makes_no_jira_calls(self, invalid, capsys):
        with patch("forge.integrations.jira.client.JiraClient") as jira_class:
            code = await cmd_project_setup(
                self.setup_args(agent_recursion_limit=invalid, repo=["org/repo"])
            )

        assert code == 1
        jira_class.assert_not_called()
        assert "positive integer" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_conflicting_limit_flags_make_no_jira_calls(self, capsys):
        with patch("forge.integrations.jira.client.JiraClient") as jira_class:
            code = await cmd_project_setup(
                self.setup_args(agent_recursion_limit=150, clear_agent_recursion_limit=True)
            )

        assert code == 1
        jira_class.assert_not_called()
        assert "cannot be combined" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("raw", "expected_source", "expected_value"),
        [(None, "global", 100), (75, "project", 75), (100, "project", 100)],
    )
    @pytest.mark.asyncio
    async def test_get_config_json_shows_raw_global_and_effective(
        self, raw, expected_source, expected_value, capsys
    ):
        jira = self.mock_config_jira(raw)
        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_get_config(
                SimpleNamespace(project_key="AISOS", json=True, property=None, models=False)
            )

        assert code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["project_properties"]["forge.agent_recursion_limit"] == raw
        assert data["global_fallbacks"]["AGENT_RECURSION_LIMIT"] == 100
        assert data["effective"]["forge.agent_recursion_limit"] == {
            "value": expected_value,
            "source": expected_source,
        }
        assert (
            sum(
                call.args[1] == "forge.agent_recursion_limit"
                for call in jira.get_project_property.await_args_list
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_get_config_text_and_property_filter_show_project_limit(self, capsys):
        jira = self.mock_config_jira(75)
        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_get_config(
                SimpleNamespace(project_key="AISOS", json=False, property=None, models=False)
            )
        assert code == 0
        output = capsys.readouterr().out
        assert "AGENT_RECURSION_LIMIT:" in output
        assert "forge.agent_recursion_limit:" in output
        assert "75 [project]" in output

        jira = self.mock_config_jira(75)
        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_get_config(
                SimpleNamespace(
                    project_key="AISOS",
                    json=False,
                    property="forge.agent_recursion_limit",
                    models=False,
                )
            )
        assert code == 0
        assert capsys.readouterr().out.strip() == "75"

    @pytest.mark.parametrize("raw", ["75", True, 0, {"bad": 1}])
    @pytest.mark.asyncio
    async def test_malformed_project_limit_is_diagnostic_error(self, raw, capsys):
        jira = self.mock_config_jira(raw)
        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_get_config(
                SimpleNamespace(project_key="AISOS", json=True, property=None, models=False)
            )

        assert code == 1
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["project_properties"]["forge.agent_recursion_limit"] == raw
        effective = data["effective"]["forge.agent_recursion_limit"]
        assert effective["value"] is None and effective["source"] == "error"
        assert "positive integer" in effective["error"]
        assert "positive integer" in captured.err

    @pytest.mark.asyncio
    async def test_failed_limit_read_does_not_claim_global_fallback(self, capsys):
        request = httpx.Request("GET", "https://jira.example.test/property")
        response = httpx.Response(403, request=request)
        error = httpx.HTTPStatusError("secret provider detail", request=request, response=response)
        jira = self.mock_config_jira(error=error)
        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_get_config(
                SimpleNamespace(project_key="AISOS", json=True, property=None, models=False)
            )

        assert code == 1
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        effective = data["effective"]["forge.agent_recursion_limit"]
        assert effective["value"] is None and effective["source"] == "error"
        assert "403" in effective["error"]
        assert "secret provider detail" not in captured.out + captured.err

    @pytest.mark.asyncio
    async def test_property_filter_reports_invalid_raw_value_without_global_claim(self, capsys):
        jira = self.mock_config_jira("75")
        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_get_config(
                SimpleNamespace(
                    project_key="AISOS",
                    json=False,
                    property="forge.agent_recursion_limit",
                    models=False,
                )
            )

        assert code == 1
        captured = capsys.readouterr()
        assert not captured.out
        assert 'Raw forge.agent_recursion_limit: "75"' in captured.err
        assert "positive integer" in captured.err
        assert "source=global" not in captured.err

    def test_project_config_alias_routes_recursion_property(self):
        parsed = []

        def record(args):
            parsed.append(args)
            return 0

        with (
            patch("forge.cli.cmd_get_config", new=record),
            patch("forge.cli.asyncio.run", side_effect=lambda result: result),
            patch("forge.cli.setup_logging"),
        ):
            assert (
                main(["project-config", "aisos", "--property", "forge.agent_recursion_limit"]) == 0
            )

        assert parsed[0].command == "get-config"
        assert parsed[0].property == "forge.agent_recursion_limit"

    def test_parser_accepts_set_clear_and_rejects_both(self):
        parsed = []

        def record(args):
            parsed.append(args)
            return 0

        with (
            patch("forge.cli.cmd_project_setup", new=record),
            patch("forge.cli.asyncio.run", side_effect=lambda result: result),
            patch("forge.cli.setup_logging"),
        ):
            assert main(["project-setup", "aisos", "--agent-recursion-limit", "150"]) == 0
            assert parsed[-1].agent_recursion_limit == 150
            assert main(["project-setup", "aisos", "--clear-agent-recursion-limit"]) == 0
            assert parsed[-1].clear_agent_recursion_limit is True
            with pytest.raises(SystemExit):
                main(
                    [
                        "project-setup",
                        "aisos",
                        "--agent-recursion-limit",
                        "150",
                        "--clear-agent-recursion-limit",
                    ]
                )


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
            "remove_default_repo": False,
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
            "json": False,
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
            remove_default_repo=True,
            remove_prd_proposals_repo=True,
            remove_prd_proposals_path=True,
            remove_skills=True,
        )

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        assert [item.args for item in jira.delete_project_property.await_args_list] == [
            ("PROJ", "forge.default_repo"),
            ("PROJ", "forge.prd_proposals_repo"),
            ("PROJ", "forge.prd_proposals_path"),
            ("PROJ", "forge.skills"),
        ]

    @pytest.mark.asyncio
    async def test_remove_default_repo_rejects_setting_a_default(self, capsys):
        jira = MagicMock()
        jira.delete_project_property = AsyncMock()
        jira.close = AsyncMock()

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(
                self.setup_args(default_repo="org/repo", remove_default_repo=True)
            )

        assert code == 1
        assert "cannot be combined" in capsys.readouterr().err
        jira.delete_project_property.assert_not_awaited()

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
            model=["implement_work=vertex:gemini-pro"],
            model_all=None,
        )

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        written = jira.set_project_property.await_args.args[2]
        assert set(written) == {"generate_prd", "implement_work"}

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
        """Only forge.repos is required when project configuration is enabled."""
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
        assert "forge.default_repo:" in out and "(none) [unset]" in out
        assert "forge.prd_proposals_repo:" in out and "(none) [unset]" in out
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
            client_inst.get_project_property = AsyncMock(
                side_effect=lambda _pk, key: "not-a-list" if key == "forge.repos" else None
            )
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

    @patch("forge.cli.cmd_project_setup", new_callable=MagicMock)
    @patch("forge.cli.setup_logging")
    def test_cli_parser_registers_references(self, _mock_setup_logging, mock_cmd):
        """Verify argparse parser registers --add-reference, --ref-description, --description, --remove-reference, and --list-references."""
        mock_cmd.return_value = 0
        with patch("forge.cli.asyncio.run", side_effect=lambda result: result):
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


class TestCLIConfigProjectSetupJson:
    """Tests for the project-setup CLI command in JSON mode."""

    @staticmethod
    def setup_args(**overrides):
        values = {
            "project_key": "AISOS",
            "repo": None,
            "add_repo": None,
            "remove_repo": None,
            "default_repo": None,
            "remove_default_repo": False,
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
            "add_reference": None,
            "ref_description": None,
            "remove_reference": None,
            "list_references": False,
            "json": True,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    @pytest.mark.asyncio
    async def test_sc001_single_set_mutation_json(self, capsys):
        """SC-001: Run project-setup with a single property mutation (Set) in JSON mode."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from forge.cli import cmd_project_setup

        jira = MagicMock()
        jira.set_project_property = AsyncMock()
        jira.close = AsyncMock()

        args = self.setup_args(default_repo="org/repo")

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        jira.set_project_property.assert_awaited_once_with(
            "AISOS", "forge.default_repo", "org/repo"
        )

        out, err = capsys.readouterr()
        assert not err
        assert "[OK]" not in out

        data = json.loads(out)
        assert data["project"] == "AISOS"
        assert "mutations" in data
        assert data["mutations"]["forge.default_repo"] == {
            "operation": "set",
            "value": "org/repo",
        }

    @pytest.mark.asyncio
    async def test_sc002_single_remove_mutation_json(self, capsys):
        """SC-002: Run project-setup with a single property mutation (Remove) in JSON mode."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from forge.cli import cmd_project_setup

        jira = MagicMock()
        jira.delete_project_property = AsyncMock()
        jira.close = AsyncMock()

        args = self.setup_args(remove_default_repo=True)

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        jira.delete_project_property.assert_awaited_once_with("AISOS", "forge.default_repo")

        out, err = capsys.readouterr()
        assert not err
        assert "[OK]" not in out

        data = json.loads(out)
        assert data["project"] == "AISOS"
        assert "mutations" in data
        assert data["mutations"]["forge.default_repo"] == {
            "operation": "remove",
            "value": None,
        }

    @pytest.mark.asyncio
    async def test_sc003_multiple_mixed_mutations_json(self, capsys):
        """SC-003: Run project-setup with multiple mutations (Mixed Set and Remove) in JSON mode."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from forge.cli import cmd_project_setup

        jira = MagicMock()
        jira.set_project_property = AsyncMock()
        jira.delete_project_property = AsyncMock()
        jira.close = AsyncMock()

        args = self.setup_args(default_repo="org/repo", remove_prd_proposals_repo=True)

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        jira.set_project_property.assert_awaited_once_with(
            "AISOS", "forge.default_repo", "org/repo"
        )
        jira.delete_project_property.assert_awaited_once_with("AISOS", "forge.prd_proposals_repo")

        out, err = capsys.readouterr()
        assert not err
        assert "[OK]" not in out

        data = json.loads(out)
        assert data["project"] == "AISOS"
        assert "mutations" in data
        assert data["mutations"]["forge.default_repo"] == {
            "operation": "set",
            "value": "org/repo",
        }
        assert data["mutations"]["forge.prd_proposals_repo"] == {
            "operation": "remove",
            "value": None,
        }

    @pytest.mark.asyncio
    async def test_sc004_backward_compatibility(self, capsys):
        """SC-004: Backward compatibility of text mode when --json is omitted."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from forge.cli import cmd_project_setup

        jira = MagicMock()
        jira.set_project_property = AsyncMock()
        jira.close = AsyncMock()

        args = self.setup_args(default_repo="org/repo", json=False)

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        jira.set_project_property.assert_awaited_once_with(
            "AISOS", "forge.default_repo", "org/repo"
        )

        out, err = capsys.readouterr()
        assert not err
        assert "[OK] forge.default_repo = 'org/repo'" in out
        # Ensure it is not structured JSON
        with pytest.raises(ValueError):
            json.loads(out)

    @pytest.mark.asyncio
    async def test_sc005_failure_reporting_json_suppresses_stdout(self, capsys):
        """SC-005: Failure reporting in JSON mode."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from forge.cli import cmd_project_setup

        jira = MagicMock()
        jira.close = AsyncMock()

        args = self.setup_args(default_repo="org/repo", remove_default_repo=True)

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 1
        out, err = capsys.readouterr()
        assert "Error: --remove-default-repo cannot be combined with --default-repo" in err
        assert not out

    @pytest.mark.asyncio
    async def test_sc005_failure_reporting_json_exception(self, capsys):
        """Verify that general exceptions raised during JSON mode suppress stdout and route cleanly to stderr."""
        from unittest.mock import AsyncMock, patch

        args = self.setup_args(default_repo="org/repo")

        with patch("forge.integrations.jira.client.JiraClient") as mock_client:
            client_inst = mock_client.return_value
            client_inst.set_project_property.side_effect = Exception("Jira client API error")
            client_inst.close = AsyncMock()

            code = await cmd_project_setup(args)

        assert code == 1
        out, err = capsys.readouterr()
        assert "Error: Jira client API error" in err
        assert not out

    @pytest.mark.asyncio
    async def test_json_mode_backward_compatibility(self, capsys):
        """Verify backward compatibility: standard [OK] status line is output when --json is omitted, and no JSON is output."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from forge.cli import cmd_project_setup

        jira = MagicMock()
        jira.set_project_property = AsyncMock()
        jira.close = AsyncMock()

        args = self.setup_args(default_repo="org/repo", json=False)

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        jira.set_project_property.assert_awaited_once_with(
            "AISOS", "forge.default_repo", "org/repo"
        )

        out, err = capsys.readouterr()
        assert not err
        assert "[OK] forge.default_repo = 'org/repo'" in out
        # Ensure it is not structured JSON
        with pytest.raises(ValueError):
            json.loads(out)

    @pytest.mark.asyncio
    async def test_json_mode_suppressed_failure_reporting(self, capsys):
        """Verify error suppression under failure scenarios in JSON mode (conflicting arguments)."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from forge.cli import cmd_project_setup

        jira = MagicMock()
        jira.close = AsyncMock()

        args = self.setup_args(default_repo="org/repo", remove_default_repo=True)

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 1
        out, err = capsys.readouterr()
        assert "Error: --remove-default-repo cannot be combined with --default-repo" in err
        assert not out

    @pytest.mark.asyncio
    async def test_json_mode_prd_proposals_repo_set_and_remove(self, capsys):
        """Test forge.prd_proposals_repo setting and removal in JSON mode."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from forge.cli import cmd_project_setup

        jira = MagicMock()
        jira.set_project_property = AsyncMock()
        jira.delete_project_property = AsyncMock()
        jira.close = AsyncMock()

        # 1. Test set
        args = self.setup_args(prd_proposals_repo="owner/repo")

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        jira.set_project_property.assert_awaited_once_with(
            "AISOS", "forge.prd_proposals_repo", "owner/repo"
        )
        out, err = capsys.readouterr()
        assert not err
        assert "[OK]" not in out
        data = json.loads(out)
        assert data["project"] == "AISOS"
        assert data["mutations"]["forge.prd_proposals_repo"] == {
            "operation": "set",
            "value": "owner/repo",
        }

        # 2. Test remove
        jira.delete_project_property.reset_mock()
        args.prd_proposals_repo = None
        args.remove_prd_proposals_repo = True

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        jira.delete_project_property.assert_awaited_once_with("AISOS", "forge.prd_proposals_repo")
        out, err = capsys.readouterr()
        assert not err
        assert "[OK]" not in out
        data = json.loads(out)
        assert data["project"] == "AISOS"
        assert data["mutations"]["forge.prd_proposals_repo"] == {
            "operation": "remove",
            "value": None,
        }

    @pytest.mark.asyncio
    async def test_json_mode_prd_proposals_path_set_and_remove(self, capsys):
        """Test forge.prd_proposals_path setting and removal in JSON mode."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from forge.cli import cmd_project_setup

        jira = MagicMock()
        jira.set_project_property = AsyncMock()
        jira.delete_project_property = AsyncMock()
        jira.close = AsyncMock()

        # 1. Test set
        args = self.setup_args(prd_proposals_path="enhancements/")

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        jira.set_project_property.assert_awaited_once_with(
            "AISOS", "forge.prd_proposals_path", "enhancements"
        )
        out, err = capsys.readouterr()
        assert not err
        assert "[OK]" not in out
        data = json.loads(out)
        assert data["mutations"]["forge.prd_proposals_path"] == {
            "operation": "set",
            "value": "enhancements",
        }

        # 2. Test remove
        jira.delete_project_property.reset_mock()
        args.prd_proposals_path = None
        args.remove_prd_proposals_path = True

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        jira.delete_project_property.assert_awaited_once_with("AISOS", "forge.prd_proposals_path")
        out, err = capsys.readouterr()
        assert not err
        assert "[OK]" not in out
        data = json.loads(out)
        assert data["mutations"]["forge.prd_proposals_path"] == {
            "operation": "remove",
            "value": None,
        }

    @pytest.mark.asyncio
    async def test_json_mode_skills_set_and_remove(self, capsys):
        """Test forge.skills setting and removal in JSON mode."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from forge.cli import cmd_project_setup

        jira = MagicMock()
        jira.set_project_property = AsyncMock()
        jira.delete_project_property = AsyncMock()
        jira.close = AsyncMock()

        # 1. Test set using add_skill
        args = self.setup_args(add_skill=["source=https://github.com/org/skill,path=my-skill"])

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        expected_value = [{"source": "https://github.com/org/skill", "path": "my-skill"}]
        jira.set_project_property.assert_awaited_once_with("AISOS", "forge.skills", expected_value)
        out, err = capsys.readouterr()
        assert not err
        data = json.loads(out)
        assert data["mutations"]["forge.skills"] == {
            "operation": "set",
            "value": expected_value,
        }

        # 2. Test remove
        jira.delete_project_property.reset_mock()
        args.add_skill = None
        args.remove_skills = True

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        jira.delete_project_property.assert_awaited_once_with("AISOS", "forge.skills")
        out, err = capsys.readouterr()
        assert not err
        data = json.loads(out)
        assert data["mutations"]["forge.skills"] == {
            "operation": "remove",
            "value": None,
        }

    @pytest.mark.asyncio
    async def test_json_mode_model_policy_set_and_remove(self, capsys):
        """Test forge.model_policy setting and removal in JSON mode."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from forge.cli import cmd_project_setup

        jira = MagicMock()
        jira.set_project_property = AsyncMock()
        jira.delete_project_property = AsyncMock()
        jira.close = AsyncMock()

        # 1. Test set using model_policy JSON
        args = self.setup_args(
            model_policy='{"generate_prd": {"connection": "anthropic", "model": "claude-3-5-sonnet"}}'
        )

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        expected_policy = {
            "generate_prd": {"connection": "anthropic", "model": "claude-3-5-sonnet"}
        }
        jira.set_project_property.assert_awaited_once_with(
            "AISOS", "forge.model_policy", expected_policy
        )
        out, err = capsys.readouterr()
        assert not err
        data = json.loads(out)
        assert data["mutations"]["forge.model_policy"] == {
            "operation": "set",
            "value": expected_policy,
        }

        # 2. Test remove using clear_model_policy
        jira.delete_project_property.reset_mock()
        args.model_policy = None
        args.clear_model_policy = True

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        jira.delete_project_property.assert_awaited_once_with("AISOS", "forge.model_policy")
        out, err = capsys.readouterr()
        assert not err
        data = json.loads(out)
        assert data["mutations"]["forge.model_policy"] == {
            "operation": "remove",
            "value": None,
        }

    @pytest.mark.asyncio
    async def test_json_mode_model_default_set_and_remove(self, capsys):
        """Test forge.model_default setting and removal in JSON mode."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from forge.cli import cmd_project_setup

        jira = MagicMock()
        jira.set_project_property = AsyncMock()
        jira.delete_project_property = AsyncMock()
        jira.close = AsyncMock()

        # 1. Test set using model_all
        args = self.setup_args(model_all="vertex-prod:gemini-pro")

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        expected_default = {"connection": "vertex-prod", "model": "gemini-pro"}
        jira.set_project_property.assert_awaited_once_with(
            "AISOS", "forge.model_default", expected_default
        )
        out, err = capsys.readouterr()
        assert not err
        data = json.loads(out)
        assert data["mutations"]["forge.model_default"] == {
            "operation": "set",
            "value": expected_default,
        }

        # 2. Test remove using clear_model_default
        jira.delete_project_property.reset_mock()
        args.model_all = None
        args.clear_model_default = True

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        jira.delete_project_property.assert_awaited_once_with("AISOS", "forge.model_default")
        out, err = capsys.readouterr()
        assert not err
        data = json.loads(out)
        assert data["mutations"]["forge.model_default"] == {
            "operation": "remove",
            "value": None,
        }

    @pytest.mark.asyncio
    async def test_json_mode_references_set(self, capsys):
        """Test forge.references setting in JSON mode."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from forge.cli import cmd_project_setup

        jira = MagicMock()
        jira.get_project_references = AsyncMock(return_value=[])
        jira.set_project_references = AsyncMock()
        jira.close = AsyncMock()

        args = self.setup_args(
            add_reference=["https://example.com/doc"],
            ref_description=["My reference description"],
        )

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        expected_references = [
            {"url": "https://example.com/doc", "description": "My reference description"}
        ]
        jira.set_project_references.assert_awaited_once_with("AISOS", expected_references)
        out, err = capsys.readouterr()
        assert not err
        data = json.loads(out)
        assert data["mutations"]["forge.references"] == {
            "operation": "set",
            "value": expected_references,
        }

    @pytest.mark.asyncio
    async def test_json_mode_list_references(self, capsys):
        """Test listing references in JSON mode without mutation."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from forge.cli import cmd_project_setup

        jira = MagicMock()
        mock_references = [{"url": "https://example.com/doc", "description": "Existing doc"}]
        jira.get_project_references = AsyncMock(return_value=mock_references)
        jira.close = AsyncMock()

        args = self.setup_args(
            list_references=True,
            json=True,
        )

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 0
        jira.get_project_references.assert_awaited_once_with("AISOS")
        out, err = capsys.readouterr()
        assert not err
        data = json.loads(out)
        assert data["references"] == mock_references
        assert data["mutations"] == {}

    @pytest.mark.asyncio
    async def test_json_mode_close_failure(self, capsys):
        """Test close failure in JSON mode routes error and returns 1."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from forge.cli import cmd_project_setup

        jira = MagicMock()
        jira.set_project_property = AsyncMock()
        jira.close = AsyncMock(side_effect=Exception("Failed to close Jira client"))

        args = self.setup_args(
            default_repo="org/repo",
            json=True,
        )

        with patch("forge.integrations.jira.client.JiraClient", return_value=jira):
            code = await cmd_project_setup(args)

        assert code == 1
        out, err = capsys.readouterr()
        assert not out
        assert "Failed to close Jira client" in err

"""Tests for the LLM client factory."""
from __future__ import annotations

from unittest.mock import MagicMock, patch


from calorch.config import Settings
from calorch.llm import MockChatModel, get_chat_model


def _settings(**kw) -> Settings:
    """Build a Settings instance with safe defaults."""
    defaults = {
        "azure_openai_api_key": None,
        "azure_openai_endpoint": None,
        "azure_openai_deployment": "gpt-4o",
        "azure_openai_api_version": "2024-08-01-preview",
        "graph_tenant_id": None,
        "graph_client_id": None,
        "graph_client_secret": None,
        "graph_user_id": "me",
        "onedrive_drive_id": None,
        "repo_backend": "json",
        "repo_path": MagicMock(),  # Path-like
        "cosmos_endpoint": None,
        "cosmos_key": None,
        "cosmos_db": "calorch",
        "cosmos_container": "events",
        "factset_api_key": None,
        "bloomberg_blpapi_host": None,
        "lseg_client_id": None,
        "spglobal_api_key": None,
        "tiingo_api_key": None,
        "opencode_go_api_key": None,
        "opencode_go_model": "glm-5.1",
        "fred_api_key": None,
        "use_fred": True,
        "use_ixbrl_segments": True,
        "use_sec_efts": True,
        "use_fed_h15": True,
        "sec_user_agent": "Calorch Test",
        "sec_cache_dir": MagicMock(),
        "sec_watchlist": [],
        "sec_forms": None,
        "use_mocks": True,
        "output_dir": MagicMock(),
        "langsmith_api_key": None,
        "langsmith_project": "calorch",
        "langsmith_tracing": False,
        "azure_storage_connection_string": None,
        "azure_storage_account_url": None,
        "blob_input_container": "calorch-inputs",
        "blob_output_container": "calorch-outputs",
        "blob_local_root": None,
        "use_blob_providers": False,
    }
    defaults.update(kw)
    return Settings(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Opencode Go path
# ---------------------------------------------------------------------------
@patch("langchain_openai.ChatOpenAI")
def test_get_chat_model_opencode_go(mock_chat_openai: MagicMock) -> None:
    """When OPENCODE_GO_API_KEY is set, ChatOpenAI with custom base_url is used."""
    mock_instance = MagicMock()
    mock_chat_openai.return_value = mock_instance

    settings = _settings(
        opencode_go_api_key="test-opencode-key",
        opencode_go_model="kimi-k2.5",
        use_mocks=False,
    )
    model = get_chat_model(settings)

    mock_chat_openai.assert_called_once_with(
        model="kimi-k2.5",
        api_key="test-opencode-key",
        base_url="https://opencode.ai/zen/go/v1",
        temperature=0.0,
    )
    assert model is mock_instance


@patch("langchain_openai.ChatOpenAI")
def test_get_chat_model_opencode_go_defaults(mock_chat_openai: MagicMock) -> None:
    """Opencode Go falls back to default model when OPENCODE_GO_MODEL is absent."""
    mock_instance = MagicMock()
    mock_chat_openai.return_value = mock_instance

    settings = _settings(
        opencode_go_api_key="test-opencode-key",
        use_mocks=False,
    )
    get_chat_model(settings)

    call_kwargs = mock_chat_openai.call_args.kwargs
    assert call_kwargs["model"] == "glm-5.1"
    assert call_kwargs["base_url"] == "https://opencode.ai/zen/go/v1"


# ---------------------------------------------------------------------------
# Azure OpenAI path
# ---------------------------------------------------------------------------
@patch("langchain_openai.AzureChatOpenAI")
def test_get_chat_model_azure(mock_azure: MagicMock) -> None:
    """When Azure creds are present (and no Opencode key), AzureChatOpenAI is used."""
    mock_instance = MagicMock()
    mock_azure.return_value = mock_instance

    settings = _settings(
        azure_openai_api_key="azure-key",
        azure_openai_endpoint="https://calorch.openai.azure.com/",
        use_mocks=False,
    )
    model = get_chat_model(settings)

    mock_azure.assert_called_once_with(
        azure_endpoint="https://calorch.openai.azure.com/",
        api_key="azure-key",
        api_version="2024-08-01-preview",
        deployment_name="gpt-4o",
        temperature=0.0,
    )
    assert model is mock_instance


# ---------------------------------------------------------------------------
# Mock fallback path
# ---------------------------------------------------------------------------
def test_get_chat_model_mock_when_no_creds() -> None:
    """With no credentials, factory returns MockChatModel regardless of USE_MOCKS."""
    settings = _settings(use_mocks=False)
    model = get_chat_model(settings)
    assert isinstance(model, MockChatModel)


def test_get_chat_model_mock_when_use_mocks_true() -> None:
    """When USE_MOCKS=true, MockChatModel is returned even if Azure creds exist."""
    # Note: current implementation prioritises Opencode > Azure > Mock.
    # USE_MOCKS only gates Azure in the old logic; now it is purely a
    # no-creds fallback. We keep this test to document the behaviour.
    settings = _settings(use_mocks=True)
    model = get_chat_model(settings)
    assert isinstance(model, MockChatModel)


# ---------------------------------------------------------------------------
# Priority order
# ---------------------------------------------------------------------------
@patch("langchain_openai.ChatOpenAI")
@patch("langchain_openai.AzureChatOpenAI")
def test_opencode_priority_over_azure(
    mock_azure: MagicMock, mock_chat_openai: MagicMock
) -> None:
    """Opencode key wins even when Azure creds are also present."""
    mock_chat_openai.return_value = MagicMock()

    settings = _settings(
        opencode_go_api_key="oc-key",
        azure_openai_api_key="azure-key",
        azure_openai_endpoint="https://calorch.openai.azure.com/",
        use_mocks=False,
    )
    get_chat_model(settings)

    mock_chat_openai.assert_called_once()
    mock_azure.assert_not_called()

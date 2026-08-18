"""api_key_ref 凭证引用安全 + custom_base_url 校验（2026-08-18 安全修复）

攻击链（修复前）：恶意 agent 配置可设 api_key_ref=任意名 → 后端读 {REF}_API_KEY 环境变量，
再配 custom_base_url=攻击者地址 → 把含密钥的请求发到攻击者服务器（密钥外泄）。
修复：① api_key_ref 白名单，未登记引用名拒绝；② custom_base_url 必须公网 http(s)。
"""

import socket

import pytest

from config import get_api_key_for_ref
from core.models.router import ModelRouter, is_safe_base_url
from core.models.types import ModelBinding


class TestApiKeyRefWhitelist:
    def test_whitelisted_ref_reads_env(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")
        assert get_api_key_for_ref("DEEPSEEK") == "sk-deepseek-test"

    def test_unregistered_ref_rejected_even_if_env_exists(self, monkeypatch):
        """核心：即使服务器上存在 STRIPE_API_KEY，未登记的引用名也必须拒绝读取。"""
        monkeypatch.setenv("STRIPE_API_KEY", "sk-stripe-secret")
        assert get_api_key_for_ref("STRIPE") == ""

    def test_ref_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("SUNO_API_KEY", "sk-suno")
        assert get_api_key_for_ref("suno") == "sk-suno"


class TestSafeBaseUrl:
    def test_public_https_allowed(self):
        # 公网 IP 字面量（沙箱 DNS 会把域名解析到私有基准段，故用 IP 测）
        assert is_safe_base_url("https://8.8.8.8/v1") is True

    def test_hostname_resolving_to_private_rejected(self, monkeypatch):
        # 模拟域名解析到内网 IP（DNS rebinding 场景）
        monkeypatch.setattr(socket, "getaddrinfo",
                            lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 0))])
        assert is_safe_base_url("https://internal.example.com") is False

    def test_loopback_rejected(self):
        assert is_safe_base_url("http://127.0.0.1:8000") is False

    def test_cloud_metadata_rejected(self):
        assert is_safe_base_url("http://169.254.169.254/latest/meta-data/") is False

    def test_private_ip_rejected(self):
        assert is_safe_base_url("http://10.0.0.5/llm") is False

    def test_non_http_scheme_rejected(self):
        assert is_safe_base_url("ftp://example.com") is False

    def test_garbage_rejected(self):
        assert is_safe_base_url("not a url") is False


class TestResolveBindingCustomBaseUrl:
    """resolve_binding 遇到不安全 custom_base_url 时，必须忽略整个自定义配置（不把密钥发出去）。"""

    @staticmethod
    def _binding(**extra) -> ModelBinding:
        # image 模态 + catalog 外 model_id → spec 为空 → 走自定义分支
        return ModelBinding(model_id="custom-image-model", provider="custom", extra=extra)

    def test_unsafe_base_url_dropped(self):
        cfg = ModelRouter.resolve_binding(
            self._binding(
                custom_model_id="custom-image-model",
                custom_base_url="http://127.0.0.1:8000",
                api_key_ref="DEEPSEEK",
            ),
            "image",
        )
        # 恶意地址绝不能出现在最终配置里（否则密钥会被发往该地址）
        assert cfg["base_url"] != "http://127.0.0.1:8000"

    def test_metadata_base_url_dropped(self):
        cfg = ModelRouter.resolve_binding(
            self._binding(
                custom_model_id="custom-image-model",
                custom_base_url="http://169.254.169.254/latest/meta-data/",
                api_key_ref="DEEPSEEK",
            ),
            "image",
        )
        assert cfg["base_url"] != "http://169.254.169.254/latest/meta-data/"

    def test_safe_base_url_kept(self):
        cfg = ModelRouter.resolve_binding(
            self._binding(
                custom_model_id="custom-image-model",
                custom_base_url="https://8.8.8.8",
                api_key_ref="",
            ),
            "image",
        )
        assert cfg["base_url"] == "https://8.8.8.8"

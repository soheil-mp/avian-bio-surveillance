from unittest.mock import ANY, patch

from avian_biosurveillance.db.minio_client import AudioStore


class TestAudioStoreEndpointResolution:
    """Tests for env-driven MinIO endpoint resolution."""

    @patch("avian_biosurveillance.db.minio_client.Minio")
    def test_falls_back_to_minio_api_port_when_endpoint_missing(self, mock_minio_cls):
        with patch.dict(
            "os.environ",
            {
                "MINIO_API_PORT": "19000",
                "MINIO_ROOT_USER": "user",
                "MINIO_ROOT_PASSWORD": "pass",
            },
            clear=False,
        ):
            import os

            os.environ.pop("MINIO_ENDPOINT", None)
            AudioStore()

        mock_minio_cls.assert_called_once_with(
            "localhost:19000",
            access_key="user",
            secret_key="pass",
            secure=False,
            http_client=ANY,
        )

    @patch("avian_biosurveillance.db.minio_client.Minio")
    def test_parses_https_endpoint_scheme(self, mock_minio_cls):
        AudioStore(
            endpoint="https://minio.example.org:9443",
            access_key="ak",
            secret_key="sk",
        )

        mock_minio_cls.assert_called_once_with(
            "minio.example.org:9443",
            access_key="ak",
            secret_key="sk",
            secure=True,
            http_client=ANY,
        )

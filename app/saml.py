from fastapi import Request
from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser
from onelogin.saml2.settings import OneLogin_Saml2_Settings

# AppConfig keys used to store the SSO configuration
SAML_CONFIG_KEYS = [
    "saml_enabled", "idp_entity_id", "idp_sso_url", "idp_slo_url",
    "idp_x509_cert", "saml_auto_provision", "saml_default_role",
]

SAML_CONFIG_DEFAULTS = {
    "saml_enabled": "false", "idp_entity_id": "", "idp_sso_url": "", "idp_slo_url": "",
    "idp_x509_cert": "", "saml_auto_provision": "true", "saml_default_role": "read_only",
}

# Claim URIs Entra ID sends by default for a user's given/family name
_ENTRA_GIVENNAME_CLAIM = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname"
_ENTRA_SURNAME_CLAIM = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname"


async def prepare_fastapi_request(request: Request) -> dict:
    """Convert a FastAPI request into the plain dict python3-saml expects."""
    form = await request.form()
    url = request.url
    return {
        "https": "on" if url.scheme == "https" else "off",
        "http_host": url.hostname,
        "server_port": str(url.port or (443 if url.scheme == "https" else 80)),
        "script_name": url.path,
        "get_data": dict(request.query_params),
        "post_data": {k: v for k, v in form.items()},
    }


def build_saml_settings(config: dict, base_url: str) -> dict:
    """Build the python3-saml settings dict. base_url is the app's own https://host:port root."""
    return {
        "strict": True,
        "debug": False,
        "sp": {
            "entityId": f"{base_url}/saml/metadata",
            "assertionConsumerService": {
                "url": f"{base_url}/saml/acs",
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "singleLogoutService": {
                "url": f"{base_url}/saml/sls",
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
        },
        "idp": {
            "entityId": config.get("idp_entity_id", ""),
            "singleSignOnService": {
                "url": config.get("idp_sso_url", ""),
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "singleLogoutService": {
                "url": config.get("idp_slo_url") or config.get("idp_sso_url", ""),
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "x509cert": config.get("idp_x509_cert", ""),
        },
        "security": {
            "wantAssertionsSigned": True,
            "wantMessagesSigned": False,
            "authnRequestsSigned": False,
        },
    }


def build_sp_metadata_xml(config: dict, base_url: str) -> tuple[str, list]:
    """Returns (metadata_xml, validation_errors) for RedTape Radar's own SP metadata."""
    settings_dict = build_saml_settings(config, base_url)
    saml_settings = OneLogin_Saml2_Settings(settings=settings_dict, sp_validation_only=True)
    metadata = saml_settings.get_sp_metadata()
    errors = saml_settings.validate_metadata(metadata)
    return metadata, errors


def parse_idp_metadata(xml_content: bytes) -> dict:
    """Parse an IdP (Entra ID) federation metadata XML into our AppConfig key/value shape."""
    parsed = OneLogin_Saml2_IdPMetadataParser.parse(xml_content)
    idp = parsed.get("idp", {})
    return {
        "idp_entity_id": idp.get("entityId", ""),
        "idp_sso_url": idp.get("singleSignOnService", {}).get("url", ""),
        "idp_slo_url": idp.get("singleLogoutService", {}).get("url", ""),
        "idp_x509_cert": idp.get("x509cert", ""),
    }


def extract_display_name(attributes: dict) -> str:
    given = (attributes.get(_ENTRA_GIVENNAME_CLAIM) or [None])[0]
    surname = (attributes.get(_ENTRA_SURNAME_CLAIM) or [None])[0]
    parts = [p for p in (given, surname) if p]
    return " ".join(parts)

"""py_webauthn glue: registration and assertion with user verification required (R64 item 4)."""

from __future__ import annotations

import json
from typing import Any

from webauthn import (generate_authentication_options, generate_registration_options,
                      options_to_json, verify_authentication_response,
                      verify_registration_response)
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)

from verify_grant import b64u, unb64u


class PasskeyError(Exception):
    """The passkey response fails a check."""


def registration_options(rp_id: str, user_id: bytes, user_name: str,
                         challenge: bytes) -> dict[str, Any]:
    opts = generate_registration_options(
        rp_id=rp_id, rp_name="awm approver", user_id=user_id, user_name=user_name,
        challenge=challenge,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED))
    return json.loads(options_to_json(opts))


def verify_registration(credential: dict, challenge: bytes, origin: str,
                        rp_id: str) -> tuple[str, str, int]:
    """(credential id, COSE public key, sign count), all ready to store."""
    try:
        v = verify_registration_response(credential=credential, expected_challenge=challenge,
                                         expected_origin=origin, expected_rp_id=rp_id,
                                         require_user_verification=True)
    except (InvalidRegistrationResponse, ValueError, KeyError) as exc:
        raise PasskeyError(f"registration refused: {exc}") from None
    return b64u(v.credential_id), b64u(v.credential_public_key), v.sign_count


def assertion_options(rp_id: str, challenge: bytes, cred_ids: list[str]) -> dict[str, Any]:
    opts = generate_authentication_options(
        rp_id=rp_id, challenge=challenge,
        allow_credentials=[PublicKeyCredentialDescriptor(id=unb64u(c)) for c in cred_ids],
        user_verification=UserVerificationRequirement.REQUIRED)
    return json.loads(options_to_json(opts))


def verify_assertion(credential: dict, challenge: bytes, origin: str, rp_id: str,
                     public_key_b64u: str, sign_count: int) -> int:
    """The new sign count. Raises PasskeyError when the assertion fails."""
    try:
        v = verify_authentication_response(
            credential=credential, expected_challenge=challenge, expected_rp_id=rp_id,
            expected_origin=origin, credential_public_key=unb64u(public_key_b64u),
            credential_current_sign_count=sign_count, require_user_verification=True)
    except (InvalidAuthenticationResponse, ValueError, KeyError) as exc:
        raise PasskeyError(f"passkey refused: {exc}") from None
    return v.new_sign_count

def test_password_hash_roundtrip():
    from app.auth import hash_password, verify_password

    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong password", hashed)


def test_session_token_roundtrip():
    from app.auth import create_session_token, read_session_token

    token = create_session_token("some-user-id")
    assert read_session_token(token) == "some-user-id"


def test_session_token_rejects_tampering():
    from app.auth import create_session_token, read_session_token

    token = create_session_token("some-user-id")
    # Flip the first character rather than the last: the last base64 group
    # can be padding-truncated, so tampering it only sometimes changes the
    # decoded bytes (~1/64 false-pass rate). An earlier character sits in a
    # full 3-byte group with no such ambiguity.
    tampered = ("a" if token[0] != "a" else "b") + token[1:]
    assert read_session_token(tampered) is None

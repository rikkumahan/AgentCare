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
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    assert read_session_token(tampered) is None

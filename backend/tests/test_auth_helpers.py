from auth import hash_password, verify_password


def test_hash_and_verify_password():
    hashed = hash_password("mypassword")
    assert hashed != "mypassword"
    assert verify_password("mypassword", hashed)
    assert not verify_password("wrongpassword", hashed)


def test_different_hashes_for_same_password():
    h1 = hash_password("same")
    h2 = hash_password("same")
    assert h1 != h2  # bcrypt includes random salt

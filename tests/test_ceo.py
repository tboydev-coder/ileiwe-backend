def admin_headers(client):
    response = client.post("/api/v1/auth/login", json={"email": "admin", "password": "admin"})
    assert response.status_code == 200, response.text
    headers = {"Authorization": "Bearer " + response.json()["access_token"]}
    changed = client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"current_password": "admin", "password": "Admin-password-2026"},
    )
    assert changed.status_code == 200, changed.text
    return {"Authorization": "Bearer " + changed.json()["access_token"]}


def test_ceo_dashboard_and_school_revocation(client):
    owner = client.post(
        "/api/v1/auth/onboard",
        json={
            "name": "School Owner",
            "email": "owner-ceo@example.com",
            "password": "Owner-password-2026",
            "school_name": "CEO Test School",
            "school_email": "school-ceo@example.com",
        },
    )
    assert owner.status_code == 201, owner.text
    headers = admin_headers(client)
    dashboard = client.get("/api/v1/ceo/dashboard", headers=headers)
    assert dashboard.status_code == 200, dashboard.text
    assert dashboard.json()["metrics"]["schools"] == 1
    schools = client.get("/api/v1/ceo/schools?search=CEO%20Test", headers=headers)
    assert schools.status_code == 200, schools.text
    school = schools.json()["items"][0]
    revoked = client.post(
        f"/api/v1/ceo/schools/{school['id']}/revoke",
        headers=headers,
        json={"confirmation": True, "reason": "Account review"},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["affected_users"] == 1
    assert client.post(
        "/api/v1/auth/login", json={"email": "owner-ceo@example.com", "password": "Owner-password-2026"}
    ).status_code == 401
    restored = client.post(
        f"/api/v1/ceo/schools/{school['id']}/restore",
        headers=headers,
        json={"confirmation": True},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["affected_users"] == 1
    owner_login = client.post(
        "/api/v1/auth/login", json={"email": "owner-ceo@example.com", "password": "Owner-password-2026"}
    )
    assert owner_login.status_code == 200, owner_login.text
    restored_school = client.get(f"/api/v1/ceo/schools/{school['id']}", headers=headers)
    assert restored_school.status_code == 200, restored_school.text
    assert restored_school.json()["school"]["status"] == "ACTIVE"
    assert restored_school.json()["users"][0]["email"] == "owner-ceo@example.com"


def test_ceo_dashboard_rejects_school_owner(client, school):
    response = client.get("/api/v1/ceo/dashboard", headers=school["headers"])
    assert response.status_code == 403

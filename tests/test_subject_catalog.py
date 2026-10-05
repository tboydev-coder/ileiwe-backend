from app.curriculum import PRIMARY_SUBJECTS, SECONDARY_SUBJECTS


def onboard(client, suffix: str, school_type: str):
    response = client.post(
        "/api/v1/auth/onboard",
        json={
            "name": "Owner",
            "email": f"{suffix}@example.com",
            "password": "Testing-password-2026!",
            "school_name": f"{suffix.title()} School",
            "school_email": f"{suffix}.school@example.com",
            "school_type": school_type,
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def list_subjects(client, headers):
    response = client.get("/api/v1/records/subjects?page_size=100", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_subject_catalog_is_seeded_for_primary_and_secondary_schools(client):
    primary_headers = onboard(client, "primary-owner", "PRIMARY")
    primary_subjects = list_subjects(client, primary_headers)
    assert len(primary_subjects) == len(PRIMARY_SUBJECTS)
    assert {s["school_level"] for s in primary_subjects} == {"PRIMARY"}

    secondary_headers = onboard(client, "secondary-owner", "SECONDARY")
    secondary_subjects = list_subjects(client, secondary_headers)
    assert len(secondary_subjects) == len(SECONDARY_SUBJECTS)
    assert {s["school_level"] for s in secondary_subjects} == {"SECONDARY"}


def test_subject_catalog_metadata_exposes_curriculum_options(client):
    headers = onboard(client, "combined-owner", "COMBINED")
    response = client.get("/api/v1/catalog", headers=headers)
    assert response.status_code == 200, response.text
    subject_fields = response.json()["subjects"]["fields"]
    name_field = next(field for field in subject_fields if field["name"] == "name")
    assert len(name_field["curriculum_options"]) == len(PRIMARY_SUBJECTS) + len(SECONDARY_SUBJECTS)
    assert {"name": PRIMARY_SUBJECTS[0][0], "level": "PRIMARY"} in name_field["curriculum_options"]
    assert {"name": SECONDARY_SUBJECTS[0][0], "level": "SECONDARY"} in name_field["curriculum_options"]

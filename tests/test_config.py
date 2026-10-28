from applier.config import PersonalProfile


def test_numeric_postal_code_is_coerced_to_string():
    profile = PersonalProfile(
        first_name="Aarush",
        last_name="Goyal",
        email="aarushg@udel.edu",
        phone="302-740-9823",
        postal_code=19702,
        linkedin="https://linkedin.com/in/aarushgoyal",
        github="https://github.com/aarushgoyal06",
        website="https://aarushspersonalwebsite.vercel.app/projects",
    )
    assert profile.postal_code == "19702"

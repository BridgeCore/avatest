import pandas as pd
import requests
import time
import re
import os
from pathlib import Path

# Get a free token at https://github.com/settings/tokens (no scopes needed)
# Set it as an env var: $env:GITHUB_TOKEN = "your_token"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}


def load_jobs(excel_path):
    df = pd.read_excel(excel_path)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    return df


def select_job(df):
    print("\nAvailable Jobs:")
    for i, row in df.iterrows():
        print(f"  {i + 1}. {row['job']}")
    while True:
        choice = input("\nEnter job number: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(df):
            return df.iloc[int(choice) - 1]
        print("Invalid choice, try again.")


def parse_skills(skills_str):
    if pd.isna(skills_str):
        return []
    return [s.strip().lower() for s in re.split(r"[,;/]+", str(skills_str)) if s.strip()]


def search_github_users(required_skills, location=None, max_results=30):
    # Use top 5 skills as search terms to keep the query focused
    skill_query = " ".join(required_skills[:5])
    query = skill_query
    if location and location.lower() not in ["remote", "n/a", "", "various"]:
        query += f" location:{location}"

    url = "https://api.github.com/search/users"
    params = {"q": query, "per_page": min(max_results, 30), "sort": "repositories"}

    resp = requests.get(url, headers=HEADERS, params=params)
    if resp.status_code == 403:
        print("GitHub rate limit hit. Add a GITHUB_TOKEN env var for higher limits.")
        return []
    if resp.status_code != 200:
        print(f"GitHub search error: {resp.status_code} — {resp.json().get('message', '')}")
        return []

    return resp.json().get("items", [])


def get_user_profile(username):
    resp = requests.get(f"https://api.github.com/users/{username}", headers=HEADERS)
    return resp.json() if resp.status_code == 200 else {}


def get_user_skills(username):
    resp = requests.get(
        f"https://api.github.com/users/{username}/repos",
        headers=HEADERS,
        params={"per_page": 50, "sort": "updated"},
    )
    if resp.status_code != 200:
        return set()

    skills = set()
    for repo in resp.json()[:20]:
        if repo.get("language"):
            skills.add(repo["language"].lower())
        for topic in repo.get("topics", []):
            skills.add(topic.lower())
        desc = str(repo.get("description") or "").lower()
        skills.update(re.split(r"\W+", desc))

    skills.discard("")
    return skills


def skill_match(candidate_skills, job_skills):
    matched = []
    for skill in job_skills:
        skill_parts = skill.split()
        if any(
            all(part in cs for part in skill_parts) or skill in cs or cs in skill
            for cs in candidate_skills
        ):
            matched.append(skill)
    return matched


def score_candidate(candidate_skills, required_skills, ideal_skills):
    req_matched = skill_match(candidate_skills, required_skills)
    ideal_matched = skill_match(candidate_skills, ideal_skills)

    req_pct = round(len(req_matched) / len(required_skills) * 100, 1) if required_skills else 0
    ideal_pct = round(len(ideal_matched) / len(ideal_skills) * 100, 1) if ideal_skills else 0
    combined = round(req_pct * 0.7 + ideal_pct * 0.3, 1)

    return {
        "required_matched": ", ".join(req_matched),
        "ideal_matched": ", ".join(ideal_matched),
        "req_pct": req_pct,
        "ideal_pct": ideal_pct,
        "combined": combined,
    }


def find_candidates(job_row):
    required_skills = parse_skills(job_row.get("required_skills", ""))
    ideal_skills = parse_skills(job_row.get("ideal_skills", ""))
    location = str(job_row.get("location", "") or "").strip()

    print(f"\nRequired skills: {', '.join(required_skills)}")
    print(f"Ideal skills:    {', '.join(ideal_skills)}")
    print(f"\nSearching GitHub...")

    users = search_github_users(required_skills, location)
    if not users:
        return []

    print(f"Found {len(users)} candidates. Fetching profiles...\n")

    results = []
    for i, user in enumerate(users):
        username = user["login"]
        print(f"  [{i + 1}/{len(users)}] {username}")

        profile = get_user_profile(username)
        repo_skills = get_user_skills(username)

        bio = str(profile.get("bio") or "").lower()
        bio_words = set(re.split(r"\W+", bio))
        candidate_skills = repo_skills | bio_words
        candidate_skills.discard("")

        scores = score_candidate(candidate_skills, required_skills, ideal_skills)

        results.append({
            "Combined Score (%)": scores["combined"],
            "% Required Match": scores["req_pct"],
            "% Ideal Match": scores["ideal_pct"],
            "GitHub Username": username,
            "Name": profile.get("name", ""),
            "Location": profile.get("location", ""),
            "Security Clearance": "Unknown",
            "Profile URL": f"https://github.com/{username}",
            "Bio": profile.get("bio", ""),
            "Required Skills Matched": scores["required_matched"],
            "Ideal Skills Matched": scores["ideal_matched"],
            "Public Repos": profile.get("public_repos", 0),
            "Followers": profile.get("followers", 0),
        })

        time.sleep(0.1)

    return sorted(results, key=lambda x: x["Combined Score (%)"], reverse=True)


def output_excel(results, job_title):
    safe_title = re.sub(r"[^\w\s-]", "", job_title).strip().replace(" ", "_")
    filename = f"candidates_{safe_title}.xlsx"

    df = pd.DataFrame(results)

    with pd.ExcelWriter(filename, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Candidates")
        ws = writer.sheets["Candidates"]
        for col in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 50)

    print(f"\nSaved: {filename}")
    return filename


def main():
    excel_path = input("Path to job descriptions Excel file: ").strip().strip('"')
    if not Path(excel_path).exists():
        print("File not found.")
        return

    df = load_jobs(excel_path)
    job = select_job(df)
    print(f"\nSelected: {job['job']}")

    candidates = find_candidates(job)

    if not candidates:
        print("No candidates found.")
        return

    print(f"\nTop 5 candidates:")
    for c in candidates[:5]:
        print(f"  {c['GitHub Username']} ({c['Name'] or 'no name'}) — {c['Combined Score (%)']}% match")

    output_excel(candidates, job["job"])


if __name__ == "__main__":
    main()

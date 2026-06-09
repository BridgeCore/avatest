import pandas as pd
import re
from pathlib import Path


def load_excel(path, label):
    path = Path(path)
    if not path.exists():
        print(f"File not found: {path}")
        return None
    df = pd.read_excel(path)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    print(f"Loaded {label}: {len(df)} rows, columns: {list(df.columns)}")
    return df


def select_job(df):
    job_col = next(
        (c for c in df.columns if c in ("job", "title", "role", "job_title", "position")),
        df.columns[0],
    )
    print(f"\nAvailable Jobs (column: '{job_col}'):")
    for i, row in df.iterrows():
        print(f"  {i + 1}. {row[job_col]}")
    while True:
        choice = input("\nEnter job number (or 'all' to match against every job): ").strip()
        if choice.lower() == "all":
            return None, job_col
        if choice.isdigit() and 1 <= int(choice) <= len(df):
            return df.iloc[int(choice) - 1], job_col
        print("Invalid choice, try again.")


def parse_skills(value):
    if pd.isna(value):
        return []
    return [s.strip().lower() for s in re.split(r"[,;|/]+", str(value)) if s.strip()]


def skill_match(candidate_skills, job_skills):
    matched = []
    for skill in job_skills:
        s = skill.lower()
        parts = s.split()
        if any(all(p in cs for p in parts) or s in cs or cs in s for cs in candidate_skills):
            matched.append(skill)
    return matched


def score_person(candidate_skills, required_skills, ideal_skills):
    req_matched = skill_match(candidate_skills, required_skills)
    ideal_matched = skill_match(candidate_skills, ideal_skills)

    req_pct = round(len(req_matched) / len(required_skills) * 100, 1) if required_skills else 100.0
    ideal_pct = round(len(ideal_matched) / len(ideal_skills) * 100, 1) if ideal_skills else 0.0
    combined = round(req_pct * 0.7 + ideal_pct * 0.3, 1) if ideal_skills else req_pct

    return {
        "req_matched": ", ".join(req_matched),
        "ideal_matched": ", ".join(ideal_matched),
        "req_pct": req_pct,
        "ideal_pct": ideal_pct,
        "combined": combined,
    }


def detect_columns(people_df):
    cols = people_df.columns.tolist()

    name_col = next(
        (c for c in cols if c in ("name", "full_name", "candidate", "person", "employee")),
        next((c for c in cols if people_df[c].dtype == object), cols[0]),
    )

    skills_col = next(
        (c for c in cols if any(k in c for k in ("skill", "tech", "expertise", "stack", "tools"))),
        None,
    )
    if not skills_col:
        print(f"\nCould not auto-detect skills column. Columns: {cols}")
        skills_col = input("Enter the column name that contains skills: ").strip().lower().replace(" ", "_")

    return name_col, skills_col


def match_people_to_job(job_row, job_col, people_df):
    required_skills = parse_skills(job_row.get("required_skills", ""))
    ideal_skills = parse_skills(job_row.get("ideal_skills", ""))

    # Fall back to any skills-like column on the job row
    if not required_skills:
        for col in job_row.index:
            if "skill" in col or "tech" in col:
                required_skills = parse_skills(job_row.get(col, ""))
                break

    print(f"\nJob: {job_row[job_col]}")
    print(f"Required skills ({len(required_skills)}): {', '.join(required_skills) or '(none)'}")
    print(f"Ideal skills    ({len(ideal_skills)}): {', '.join(ideal_skills) or '(none)'}")

    name_col, skills_col = detect_columns(people_df)
    print(f"\nScoring {len(people_df)} people...\n")

    results = []
    for _, row in people_df.iterrows():
        candidate_skills = set(parse_skills(row.get(skills_col, "")))
        scores = score_person(candidate_skills, required_skills, ideal_skills)

        record = {
            "Combined Score (%)": scores["combined"],
            "% Required Match": scores["req_pct"],
            "% Ideal Match": scores["ideal_pct"] if ideal_skills else "N/A",
            "Name": row.get(name_col, ""),
            "Required Skills Matched": scores["req_matched"],
            "Ideal Skills Matched": scores["ideal_matched"] if ideal_skills else "N/A",
            "All Candidate Skills": row.get(skills_col, ""),
        }

        # Carry over any extra columns (email, location, etc.)
        for col in people_df.columns:
            if col not in (name_col, skills_col) and col not in record:
                record[col] = row.get(col, "")

        results.append(record)

    return sorted(results, key=lambda x: x["Combined Score (%)"], reverse=True)


def save_results(results, job_title):
    safe = re.sub(r"[^\w\s-]", "", str(job_title)).strip().replace(" ", "_")
    filename = f"matches_{safe}.xlsx"
    df = pd.DataFrame(results)

    with pd.ExcelWriter(filename, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Ranked Candidates")
        ws = writer.sheets["Ranked Candidates"]
        for col in ws.columns:
            max_len = max((len(str(cell.value or "")) for cell in col), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 60)

    print(f"Saved: {filename}")


def print_top(results, job_title, n=5):
    print(f"\nTop {min(n, len(results))} matches for '{job_title}':")
    for r in results[:n]:
        print(f"  {str(r['Name']):30s}  {r['Combined Score (%)']:5.1f}%  matched: {r['Required Skills Matched'] or '(none)'}")


def main():
    print("=== Skills Matcher ===\n")
    jobs_path = input("Path to job descriptions Excel file: ").strip().strip('"')
    people_path = input("Path to people/skills Excel file:   ").strip().strip('"')

    jobs_df = load_excel(jobs_path, "jobs")
    people_df = load_excel(people_path, "people")
    if jobs_df is None or people_df is None:
        return

    job_row, job_col = select_job(jobs_df)

    if job_row is None:
        for _, row in jobs_df.iterrows():
            results = match_people_to_job(row, job_col, people_df)
            print_top(results, row[job_col])
            save_results(results, row[job_col])
    else:
        results = match_people_to_job(job_row, job_col, people_df)
        print_top(results, job_row[job_col])
        save_results(results, job_row[job_col])


if __name__ == "__main__":
    main()

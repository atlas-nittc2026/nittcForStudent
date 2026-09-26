"""MCC Web Syllabus の学科・科目・詳細シラバスを SQLite に保存する。"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = "https://syllabus.kosen-k.go.jp"
DEFAULT_SCHOOL_ID = 15
DEFAULT_YEAR = 2026
DEFAULT_LANGUAGE = "ja"
DEFAULT_DATABASE = "syllabus.db"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Department:
	school_id: int
	department_id: int
	name: str
	year: int


def clean_text(value: str) -> str:
	return re.sub(r"\s+", " ", value).strip()


def query_value(url: str, name: str) -> str:
	values = parse_qs(urlparse(url).query).get(name, [""])
	return values[0]


def make_session() -> requests.Session:
	retry = Retry(
		total=3,
		backoff_factor=1,
		status_forcelist=(429, 500, 502, 503, 504),
		allowed_methods=frozenset({"GET"}),
	)
	session = requests.Session()
	session.mount("https://", HTTPAdapter(max_retries=retry))
	session.headers.update(
		{"User-Agent": "kosen-syllabus-scraper/1.0 (educational use)"}
	)
	return session


def fetch(session: requests.Session, url: str, delay: float) -> BeautifulSoup:
	LOGGER.info("GET %s", url)
	response = session.get(url, timeout=30)
	response.raise_for_status()
	response.encoding = "utf-8"
	if delay > 0:
		time.sleep(delay)
	return BeautifulSoup(response.content.decode("utf-8-sig"), "html.parser")


def create_database(connection: sqlite3.Connection) -> None:
	connection.text_factory = str
	if not connection.execute("PRAGMA encoding").fetchone()[0].upper().endswith("UTF-8"):
		raise RuntimeError("SQLiteデータベースがUTF-8ではありません")
	connection.executescript(
		"""
		PRAGMA encoding = "UTF-8";
		PRAGMA foreign_keys = ON;
		CREATE TABLE IF NOT EXISTS departments (
			school_id INTEGER NOT NULL,
			department_id INTEGER NOT NULL,
			year INTEGER NOT NULL,
			name TEXT NOT NULL,
			source_url TEXT NOT NULL,
			PRIMARY KEY (school_id, department_id, year)
		);
		CREATE TABLE IF NOT EXISTS subjects (
			school_id INTEGER NOT NULL,
			department_id INTEGER NOT NULL,
			year INTEGER NOT NULL,
			subject_code TEXT NOT NULL,
			subject_name TEXT NOT NULL,
			category TEXT,
			requirement TEXT,
			credit_type TEXT,
			credits TEXT,
			teachers TEXT,
			weekly_hours_json TEXT,
			grade TEXT NOT NULL DEFAULT '',
			semester TEXT NOT NULL DEFAULT '[]',
			source_url TEXT NOT NULL,
			PRIMARY KEY (school_id, department_id, year, subject_code),
			FOREIGN KEY (school_id, department_id, year)
				REFERENCES departments (school_id, department_id, year)
				ON DELETE CASCADE
		);
		CREATE TABLE IF NOT EXISTS syllabus_sections (
			school_id INTEGER NOT NULL,
			department_id INTEGER NOT NULL,
			year INTEGER NOT NULL,
			subject_code TEXT NOT NULL,
			section_name TEXT NOT NULL,
			section_order INTEGER NOT NULL,
			content_json TEXT NOT NULL,
			source_url TEXT NOT NULL,
			PRIMARY KEY (
				school_id, department_id, year, subject_code, section_order
			),
			FOREIGN KEY (school_id, department_id, year, subject_code)
				REFERENCES subjects (
					school_id, department_id, year, subject_code
				) ON DELETE CASCADE
		);
		"""
	)
	columns = {
		row[1] for row in connection.execute("PRAGMA table_info(subjects)")
	}
	if "grade" not in columns:
		connection.execute(
			"ALTER TABLE subjects ADD COLUMN grade TEXT NOT NULL DEFAULT ''"
		)
	if "semester" not in columns:
		connection.execute(
			"ALTER TABLE subjects ADD COLUMN semester TEXT NOT NULL DEFAULT '[]'"
		)
	if "grades_json" in columns:
		connection.execute("ALTER TABLE subjects DROP COLUMN grades_json")
	connection.execute("DROP TABLE IF EXISTS subject_grades")


def subject_grade(weekly_hours: list[str], department_name: str) -> str:
	grade_prefix = "a" if department_name.startswith("【専攻科】") else ""
	grades = [
		f"{grade_prefix}{1 + index // 4}"
		for index in range(0, len(weekly_hours), 4)
		if any(weekly_hours[index:index + 4])
	]
	if len(grades) != 1:
		raise ValueError(f"科目の学年を一意に判定できません: {grades}")
	return grades[0]


def subject_semesters(weekly_hours: list[str], department_name: str) -> list[int]:
	grade_blocks = [
		index for index in range(0, len(weekly_hours), 4)
		if any(weekly_hours[index:index + 4])
	]
	if len(grade_blocks) != 1:
		raise ValueError(f"科目の学年を一意に判定できません: {grade_blocks}")
	grade_start = grade_blocks[0]
	return [
		1 + offset // 2
		for offset in range(0, 4, 2)
		if any(weekly_hours[grade_start + offset:grade_start + offset + 2])
	]


def backfill_subjects(connection: sqlite3.Connection) -> None:
	columns = ("school_id", "department_id", "year", "subject_code",
			   "weekly_hours_json", "department_name")
	for subject in connection.execute(
		"SELECT subjects.school_id, subjects.department_id, subjects.year, "
		"subjects.subject_code, subjects.weekly_hours_json, departments.name "
		"FROM subjects JOIN departments USING (school_id, department_id, year)"
	):
		values = dict(zip(columns, subject))
		weekly_hours = json.loads(values["weekly_hours_json"])
		connection.execute(
			"UPDATE subjects SET grade = ?, semester = ? WHERE school_id = ? "
			"AND department_id = ? AND year = ? AND subject_code = ?",
			(
				subject_grade(weekly_hours, values["department_name"]),
				json.dumps(subject_semesters(weekly_hours, values["department_name"])),
				values["school_id"], values["department_id"],
				values["year"], values["subject_code"],
			),
		)


def get_departments(
	session: requests.Session, school_id: int, year: int, language: str, delay: float
) -> list[Department]:
	url = (
		f"{BASE_URL}/Pages/PublicDepartments?school_id={school_id}"
		f"&year={year}&lang={language}"
	)
	soup = fetch(session, url, delay)
	departments: list[Department] = []
	seen: set[int] = set()
	for link in soup.select('a[href*="PublicSubjects"]'):
		href = urljoin(BASE_URL, link.get("href", ""))
		department_id = query_value(href, "department_id")
		if not department_id or int(department_id) in seen:
			continue
		department_row = link.find_parent("div", class_="row")
		name_element = department_row.select_one("h4") if department_row else None
		name = clean_text(name_element.get_text(" ", strip=True)) if name_element else ""
		if not name:
			continue
		seen.add(int(department_id))
		departments.append(
			Department(
				school_id=school_id,
				department_id=int(department_id),
				name=name,
				year=year,
			)
		)
	if not departments:
		raise RuntimeError("学科一覧を取得できませんでした。URLまたはサイト構造を確認してください。")
	return departments


def table_rows(table: Tag) -> list[list[str]]:
	return [
		[clean_text(cell.get_text(" ", strip=True)) for cell in row.select("th, td")]
		for row in table.select("tr")
		if row.select("th, td")
	]


def parse_subjects(
	soup: BeautifulSoup, department: Department, language: str
) -> list[dict[str, Any]]:
	subjects: list[dict[str, Any]] = []
	for link in soup.select('a[href*="PublicSyllabus"]'):
		href = urljoin(BASE_URL, link.get("href", ""))
		subject_code = query_value(href, "subject_code")
		subject_id = query_value(href, "subject_id")
		code = subject_code or subject_id
		if not code:
			continue
		row = link.find_parent("tr")
		if row is None:
			continue
		cells = [clean_text(cell.get_text(" ", strip=True)) for cell in row.select("td")]
		if len(cells) < 6:
			continue
		subjects.append(
			{
				"school_id": department.school_id,
				"department_id": department.department_id,
				"year": department.year,
				"subject_code": code,
				"subject_name": clean_text(link.get_text(" ", strip=True)),
				"category": cells[0],
				"requirement": cells[1],
				"credit_type": cells[4],
				"credits": cells[5],
				"teachers": cells[-2] if len(cells) >= 2 else "",
				"weekly_hours_json": json.dumps(cells[6:-2], ensure_ascii=False),
				"grade": subject_grade(cells[6:-2], department.name),
				"semester": json.dumps(
					subject_semesters(cells[6:-2], department.name)
				),
				"source_url": href,
			}
		)
	unique: dict[str, dict[str, Any]] = {
		subject["subject_code"]: subject for subject in subjects
	}
	return list(unique.values())


def section_content(heading: Tag) -> dict[str, Any]:
	paragraphs: list[str] = []
	tables: list[list[list[str]]] = []
	current = heading.find_next_sibling()
	while current is not None and not (
		isinstance(current, Tag) and current.name in {"h1", "h2", "h3"}
	):
		if isinstance(current, Tag):
			for table in current.select("table") if current.name != "table" else [current]:
				rows = table_rows(table)
				if rows:
					tables.append(rows)
			if current.name != "table":
				text = clean_text(current.get_text(" ", strip=True))
				if text and not current.find("table"):
					paragraphs.append(text)
		current = current.find_next_sibling()
	return {"paragraphs": paragraphs, "tables": tables}


def parse_syllabus(soup: BeautifulSoup) -> list[dict[str, Any]]:
	sections: list[dict[str, Any]] = []
	for heading in soup.select("h3"):
		name = clean_text(heading.get_text(" ", strip=True))
		if not name:
			continue
		sections.append(
			{
				"section_name": name,
				"section_order": len(sections),
				"content": section_content(heading),
			}
		)
	return sections


def save_department(connection: sqlite3.Connection, department: Department, url: str) -> None:
	connection.execute(
		"""INSERT OR REPLACE INTO departments
		   (school_id, department_id, year, name, source_url)
		   VALUES (?, ?, ?, ?, ?)""",
		(department.school_id, department.department_id, department.year, department.name, url),
	)


def save_subject(connection: sqlite3.Connection, subject: dict[str, Any]) -> None:
	connection.execute(
		"""INSERT OR REPLACE INTO subjects
		   (school_id, department_id, year, subject_code, subject_name,
			category, requirement, credit_type, credits, teachers,
			weekly_hours_json, grade, semester, source_url)
		   VALUES (:school_id, :department_id, :year, :subject_code,
				   :subject_name, :category, :requirement, :credit_type,
				   :credits, :teachers, :weekly_hours_json, :grade, :semester,
				   :source_url)""",
		subject,
	)


def save_sections(connection: sqlite3.Connection, subject: dict[str, Any], sections: Iterable[dict[str, Any]]) -> None:
	connection.execute(
		"""DELETE FROM syllabus_sections
		   WHERE school_id = ? AND department_id = ? AND year = ? AND subject_code = ?""",
		(subject["school_id"], subject["department_id"], subject["year"], subject["subject_code"]),
	)
	connection.executemany(
		"""INSERT INTO syllabus_sections
		   (school_id, department_id, year, subject_code, section_name,
			section_order, content_json, source_url)
		   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
		(
			(
				subject["school_id"],
				subject["department_id"],
				subject["year"],
				subject["subject_code"],
				section["section_name"],
				section["section_order"],
				json.dumps(section["content"], ensure_ascii=False),
				subject["source_url"],
			)
			for section in sections
		),
	)


def fetch_subject_syllabus(subject: dict[str, Any], delay: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
	subject_session = make_session()
	syllabus_soup = fetch(subject_session, subject["source_url"], delay)
	return subject, parse_syllabus(syllabus_soup)


def scrape(
	school_id: int,
	year: int,
	language: str,
	database: str,
	delay: float,
	max_workers: int,
) -> None:
	session = make_session()
	departments = get_departments(session, school_id, year, language, delay)
	with sqlite3.connect(database) as connection:
		create_database(connection)
		backfill_subjects(connection)
		connection.commit()
		for department in departments:
			department_url = (
				f"{BASE_URL}/Pages/PublicSubjects?school_id={school_id}"
				f"&department_id={department.department_id}&year={year}&lang={language}"
			)
			subject_soup = fetch(session, department_url, delay)
			save_department(connection, department, department_url)
			subjects = parse_subjects(subject_soup, department, language)
			if not subjects:
				raise RuntimeError(
					f"{department.name} (department_id={department.department_id}) "
					"の科目一覧が空です。アクセス制限またはサイト構造を確認してください。"
				)
			LOGGER.info("%s: %d科目", department.name, len(subjects))
			for subject in subjects:
				save_subject(connection, subject)
			connection.commit()
			with ThreadPoolExecutor(max_workers=max_workers) as executor:
				futures = {
					executor.submit(fetch_subject_syllabus, subject, delay): subject
					for subject in subjects
				}
				for future in as_completed(futures):
					subject = futures[future]
					try:
						_, sections = future.result()
					except requests.RequestException as error:
						LOGGER.warning(
							"詳細取得失敗 subject_code=%s: %s",
							subject["subject_code"],
							error,
						)
						continue
					save_sections(connection, subject, sections)
			connection.commit()


def main() -> None:
	parser = argparse.ArgumentParser(description="高専WebシラバスをSQLiteへ保存します")
	parser.add_argument("--school-id", type=int, default=DEFAULT_SCHOOL_ID)
	parser.add_argument("--year", type=int, default=DEFAULT_YEAR)
	parser.add_argument("--language", default=DEFAULT_LANGUAGE)
	parser.add_argument("--database", default=DEFAULT_DATABASE)
	parser.add_argument("--delay", type=float, default=0.5, help="リクエスト間隔（秒）")
	parser.add_argument("--max-workers", type=int, default=4, help="詳細ページ取得の並列数")
	parser.add_argument("--verbose", action="store_true")
	args = parser.parse_args()
	if args.max_workers < 1:
		parser.error("--max-workers は1以上を指定してください")
	logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
	scrape(args.school_id, args.year, args.language, args.database, args.delay, args.max_workers)


if __name__ == "__main__":
	main()

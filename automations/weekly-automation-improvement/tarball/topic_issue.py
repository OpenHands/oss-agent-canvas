import hashlib
import json
import re
from pathlib import Path

from common import read_json, redact, write_json
from github_io import command, github_pages, IMPROVEMENT_LABEL
from raw_evidence import schema, PAGE

SECTIONS = (
    "Problem and impact",
    "Observed example",
    "Proposed prevention",
    "Validation",
    "Current status",
    "Evidence",
    "Attempts and decisions",
)
READ_SECTIONS = (*SECTIONS, "Goal", "Scope and source")
ISSUE_GUIDANCE = (
    "The current-run issue brief is a living public explanation, understandable and reviewable by an engineer with GitHub access only, without Laminar or VM access. For a fresh issue it is the body; on an existing issue it is ONE comment you create for this run. "
    "Use these exact level-two sections in order: Problem and impact; Observed example; Proposed prevention; Validation; Current status; Evidence; Attempts and decisions. "
    "Before Evidence, name the affected automation/component in plain language, explain expected versus observed behavior and consequences, give a sanitized concrete example, and distinguish confirmed facts from hypotheses. "
    "Explain how the proposed prevention addresses the mechanism, its scope and tradeoffs. State observable acceptance criteria, a concise public before/after result, available/tested counts and limitations; keep commands, hashes, raw identifiers and VM paths in Evidence. "
    "Maintain ONE current brief. During this run, read the current issue and comments, then rewrite the sections of your current-run body or brief comment rather than appending a Current repair attempt, dated continuation, competing summary or final-result appendix. Never rewrite an existing human-authored or previous-run issue description. "
    "Revise the same sections after diagnosis, a changed hypothesis, validation and independent review. Correct or remove disproved claims and obsolete blockers; Current status must reflect the latest verdict, PRs and remaining work. "
    "Preserve human-authored text and comments, including any HUMAN template section. Preserve all ownership markers, evidence/catalog anchors and links needed to retrieve past records. "
    "Before rewriting your brief, save its full prior body or comment privately under validation/issue-history/ with a unique filename, so the evidence catalog retains its exact text. Keep historical evidence roots/access references in the private catalogs; include them in Evidence only when safe for the destination repository. Prior attempts belong in comments/catalogs with a concise historical link, not repeated narratives in the current brief. "
    "Never publish raw tasks, private quotations, credentials or sensitive customer details. Safe summaries and anonymized examples are required, not blanket omission of the explanation. "
)


def public_issue_errors(body):
    headings = list(re.finditer(r"(?m)^## ([^\n]+?)[ \t]*$", body))
    names = [match[1] for match in headings]
    errors = []
    if any(names.count(name) != 1 for name in SECTIONS):
        return ["Issue needs one of each public section: " + "; ".join(SECTIONS)]
    positions = [names.index(name) for name in SECTIONS]
    if positions != sorted(positions):
        errors.append("Issue must explain the problem and prevention before evidence")
    for name in SECTIONS:
        index = names.index(name)
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        text = re.sub(r"<!--[\s\S]*?-->", "", body[headings[index].end() : end])
        if not text.strip():
            errors.append("Issue section is empty: " + name)
    public = body[: headings[names.index("Evidence")].start()]
    if re.search(r"/(?:var/lib|tmp|home|Users)/|[a-f0-9]{64}\.json", public):
        errors.append("Move private artifact paths/references into Evidence")
    if any(
        value in body
        for value in (
            "Supporting observations and impact assessments are in the private evidence records linked below.",
            "Investigate this topic and propose one focused repair",
            "No public example has been written yet.",
        )
    ):
        errors.append(
            "Replace the generic investigation placeholder with a public explanation"
        )
    return errors


RECORD = "<!-- automation-improvement-record:"


def brief(snapshot, evidence_root, location):
    source, topic = snapshot["source"], snapshot["topic"]
    links = []
    for sample in snapshot["samples"]:
        for trace in sample.get("trace_ids", [sample.get("trace_id")]):
            if trace:
                links.append(
                    f"- [Conversation {sample['conversation_id']}; trace {trace}]({(snapshot.get('app_url') or 'https://laminar.app.all-hands.dev').rstrip('/')}/project/{snapshot['project_id']}/traces/{trace})"
                )
        links.extend(
            f"- Signal event `{identifier}` (read with `read_signal`)."
            for identifier in sample.get("selected_event_ids", [])
        )
    triage_issue = (snapshot.get("aggregation") or {}).get("triage_issue") or {}
    if triage_issue.get("url"):
        links.append(f"- [Weekly triage and priority context]({triage_issue['url']}).")
    texts = (
        f"Unconfirmed investigation lead: **{topic.get('title') or topic['key'].replace('-', ' ')}**. The public diagnosis must distinguish observed behavior from an inferred cause.",
        "No public example has been written yet. The investigating agent must summarize a concrete observation without exposing private content.",
        "Prevention is not selected yet. Investigate the actual source and describe a focused correction and its tradeoffs before proposing a repair.",
        f"Use up to N={snapshot.get('validation_cohort', {}).get('sample_size', 5)} negative and random non-negative original inputs when available, without replacements. State public acceptance criteria, results and limitations; distinguish mechanism checks from historical task replay.",
        "Investigation pending; no fix or improvement has been established.",
        f"Repository: {source['repository']}. Initial baseline: `{source['revision']}`; this does not prove historical execution provenance.\n\n"
        + "\n".join(links)
        + f"\n\nPrivate evidence: `{evidence_root}`. "
        + (f"Worker access: {location}. " if location else "")
        + "Use read_issue_artifact for catalog references and scoped Laminar tools for original data. Raw tasks, prompts and logs remain private.\n\n<!-- topic-evidence:start -->\n<!-- topic-evidence:end -->",
        "Earlier attempts and immutable evidence remain in the issue comments. Keep the current diagnosis and verdict in the sections above.\n\n<!-- topic-attempts:start -->\n<!-- topic-attempts:end -->",
    )
    return f"<!-- automation-improvement-topic: {topic['id']} -->\n\n" + "\n\n".join(
        f"## {section}\n\n{text}" for section, text in zip(SECTIONS, texts, strict=True)
    )


class TopicIssue:
    def __init__(
        self,
        repository,
        output,
        evidence_root,
        value,
        comments,
        preview=False,
        brief_comment_id=None,
    ):
        self.repository, self.output, self.evidence_root = (
            repository,
            output,
            evidence_root,
        )
        self.value, self.comments, self.preview = value, comments, preview
        self.endpoint = f"repos/{repository}/issues/{value['number']}"
        self.brief_comment_id = brief_comment_id
        self.sequence = 0

    @classmethod
    def ensure(cls, snapshot, output, preview=False):
        repository = snapshot["source"]["repository"]
        topic = snapshot["topic"]
        if (
            not re.fullmatch(r"[\w.-]+/[\w.-]+", repository)
            or not re.fullmatch(r"[a-z0-9-]+", topic["id"])
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", topic["key"])
        ):
            raise ValueError("Invalid repair issue scope")
        evidence_root = Path(snapshot["issue_evidence_dir"]).expanduser().resolve()
        body = redact(brief(snapshot, evidence_root, snapshot.get("evidence_location")))
        return cls.ensure_brief(
            repository,
            output,
            evidence_root,
            body,
            "Automation improvement: " + topic["key"].replace("-", " "),
            preview,
        )

    @classmethod
    def ensure_brief(
        cls, repository, output, evidence_root, body, title, preview=False
    ):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise ValueError("Invalid issue repository")
        if preview:
            return cls(
                repository,
                output,
                evidence_root,
                {"number": 0, "html_url": None, "body": body, "state": "open"},
                [],
                True,
            )
        rows = github_pages(
            f"repos/{repository}/issues?state=all&labels={IMPROVEMENT_LABEL}&per_page=100",
            command,
        )
        marker = body.splitlines()[0]
        found = [
            item
            for item in rows
            if not item.get("pull_request") and marker in (item.get("body") or "")
        ]
        if len(found) > 1:
            raise ValueError("Multiple issues own this repair topic")
        if found:
            value = found[0]
        else:
            payload = output / "create-issue.json"
            write_json(
                payload,
                {
                    "title": title,
                    "body": body,
                    "labels": [IMPROVEMENT_LABEL],
                },
            )
            value = json.loads(
                command(
                    [
                        "gh",
                        "api",
                        "--method",
                        "POST",
                        f"repos/{repository}/issues",
                        "--input",
                        str(payload),
                    ]
                )
            )
        if (
            value["html_url"]
            != f"https://github.com/{repository}/issues/{value['number']}"
        ):
            raise ValueError("Repair issue belongs to a different repository")
        comments = github_pages(
            f"repos/{repository}/issues/{value['number']}/comments?per_page=100",
            command,
        )
        return cls(
            repository,
            output,
            evidence_root,
            value,
            comments,
        )

    def refresh(self):
        if not self.preview:
            self.value = json.loads(command(["gh", "api", self.endpoint]))
            self.comments = github_pages(
                self.endpoint + "/comments?per_page=100", command
            )
            if self.brief_comment_id is not None:
                brief = next(
                    item
                    for item in self.comments
                    if item["id"] == self.brief_comment_id
                )
                self.value["body"] = brief["body"]
        return self.context()

    def context(self):
        return {
            "url": self.value["html_url"],
            "number": self.value["number"],
            "body": self.value["body"],
            "brief_comment_id": self.brief_comment_id,
            "sections": list(READ_SECTIONS),
            "records": [
                {
                    "id": item["id"],
                    "url": item.get("html_url"),
                    "heading": next(
                        (
                            line
                            for line in item["body"].splitlines()
                            if line.startswith("## ")
                        ),
                        "Discussion",
                    ),
                }
                for item in self.comments
            ],
        }

    def artifact(self, name, value):
        value = redact(value)
        text = json.dumps(value, ensure_ascii=False, indent=2)
        if len(text.encode()) > 32_000_000:
            raise ValueError("Private evidence exceeds its byte budget")
        digest = hashlib.sha256(text.encode()).hexdigest()
        reference = digest + ".json"
        path = self.evidence_root / reference
        if path.is_symlink():
            raise ValueError("Private evidence must be a regular file")
        if not path.exists():
            write_json(path, value)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Private evidence was modified")
        return {"name": name, "reference": reference}

    def update_body(self, body):
        if redact(body) != body:
            raise ValueError(
                "Issue body contains a possible credential; sanitize before publishing"
            )
        if self.preview:
            self.value["body"] = body
            return
        self.sequence += 1
        payload = self.output / f"issue-body-{self.sequence:04d}.json"
        write_json(payload, {"body": body})
        endpoint = (
            f"repos/{self.repository}/issues/comments/{self.brief_comment_id}"
            if self.brief_comment_id is not None
            else self.endpoint
        )
        value = json.loads(
            command(
                [
                    "gh",
                    "api",
                    "--method",
                    "PATCH",
                    endpoint,
                    "--input",
                    str(payload),
                ]
            )
        )
        if self.brief_comment_id is None:
            self.value = value
        else:
            self.value["body"] = value["body"]

    def update_block(self, name, content):
        self.refresh()
        start, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
        body = self.value["body"]
        if (
            body.count(start) != 1
            or body.count(end) != 1
            or body.index(start) >= body.index(end)
        ):
            raise ValueError("Central issue record section is missing or ambiguous")
        updated = (
            body[: body.index(start) + len(start)]
            + "\n"
            + content.strip()
            + "\n"
            + body[body.index(end) :]
        )
        if updated != body:
            self.update_body(updated)

    def related(self, urls):
        self.refresh()
        body = self.value["body"]
        start, end = "<!-- topic-related:start -->", "<!-- topic-related:end -->"
        if start not in body and end not in body:
            anchor = "<!-- topic-evidence:start -->"
            if body.count(anchor) != 1:
                raise ValueError(
                    "Central issue evidence section is missing or ambiguous"
                )
            self.update_body(
                body.replace(anchor, start + "\n" + end + "\n\n" + anchor, 1)
            )
        self.update_block(
            "topic-related",
            "### Related repair work\n\n"
            + "\n".join("- " + url for url in sorted(set(urls))),
        )

    def read_artifact(self, reference):
        if not isinstance(reference, str) or not re.fullmatch(
            r"[a-f0-9]{64}\.json", reference
        ):
            raise ValueError("Invalid evidence reference")
        path = self.evidence_root / reference
        if path.is_symlink() or not path.is_file():
            raise ValueError("Evidence is missing or outside this repair topic")
        if path.stat().st_size > 32_000_000:
            raise ValueError("Evidence exceeds its byte budget")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != reference[:-5]:
            raise ValueError("Private evidence was modified")
        return data.decode()

    def record(self, title, summary, artifacts):
        catalog = [self.artifact(name, value) for name, value in artifacts.items()]
        index = self.artifact("evidence-index.json", catalog)
        content = (
            f"## {title}\n\n{summary}\n\n### Private evidence catalog\n\n"
            f"The complete index contains {len(catalog)} artifacts. Read it with "
            "`read_issue_artifact(reference, offset)`, then use the same tool for each indexed reference. "
            "Raw content and storage/access details stay on the authorized automation worker.\n\n| Evidence | Reference |\n|---|---|\n"
            f"| evidence-index.json | `{index['reference']}` |"
        )
        if len(content) > 60000:
            raise ValueError("Issue attempt summary exceeds its size budget")
        if redact(content) != content:
            raise ValueError("Public evidence summary contains a possible credential")
        marker = f"{RECORD} {hashlib.sha256(content.encode()).hexdigest()} -->"
        found = [item for item in self.comments if item["body"].startswith(marker)]
        if found:
            item = found[0]
        else:
            self.sequence += 1
            payload = self.output / f"comment-{self.sequence:04d}.json"
            write_json(payload, {"body": marker + "\n" + content})
            item = (
                {"id": self.sequence, "html_url": None, "body": marker + "\n" + content}
                if self.preview
                else json.loads(
                    command(
                        [
                            "gh",
                            "api",
                            "--method",
                            "POST",
                            self.endpoint + "/comments",
                            "--input",
                            str(payload),
                        ]
                    )
                )
            )
            self.comments.append(item)
        kind = "evidence" if title.startswith("Evidence") else "attempts"
        link = f"- [{title}]({item.get('html_url') or '#preview'}) — comment `{item['id']}`."
        self.update_block("topic-" + kind, link)
        return {"id": item["id"], "url": item.get("html_url")}

    def frozen(self, snapshot):
        artifacts = {
            "original-context.json": {
                key: snapshot.get(key)
                for key in ("project_id", "source", "topic", "aggregation")
            }
        }
        artifacts["original-context.json"]["samples"] = [
            {
                key: sample.get(key)
                for key in (
                    "case_id",
                    "automation_id",
                    "conversation_id",
                    "trace_ids",
                    "selected_event_ids",
                    "initial_instructions",
                    "instruction_coverage",
                    "source_resolution",
                    "flow1_events",
                )
            }
            for sample in snapshot["samples"]
        ]
        if snapshot.get("validation_cohort"):
            cohort = read_json(Path(snapshot["validation_cohort"]["path"]), 32_000_000)
            artifacts["validation-inputs.json"] = {
                "scope": cohort.get("scope"),
                "selection": cohort.get("selection"),
                "errors": cohort["errors"],
                "cases": [
                    {
                        key: sample.get(key)
                        for key in (
                            "case_id",
                            "classification",
                            "conversation_id",
                            "trace_id",
                            "signal_event_id",
                            "native_payload",
                            "repository_snapshot",
                            "github_reads",
                        )
                    }
                    for sample in cohort["samples"]
                ],
            }
        return self.record(
            "Evidence — frozen context",
            f"Frozen source baseline: `{snapshot['source']['revision']}`. Validation sample parameter N={snapshot.get('validation_cohort', {}).get('sample_size', 5)}. Original task, approved source and selected validation inputs are preserved privately. Production traces are linked in the issue's Evidence section.",
            artifacts,
        )

    def attempt(self, output, report, publication=None):
        artifacts = {
            "investigation.json": {
                key: report.get(key)
                for key in ("findings", "rejected_findings", "proposals", "errors")
            }
        }
        coding = output / "coding-agent"
        if coding.exists():
            for path in sorted(coding.glob("*.json")):
                artifacts[str(path.relative_to(output))] = read_json(path, 32_000_000)
            for path in sorted((coding / "completions").glob("*.json")):
                artifacts[str(path.relative_to(output))] = read_json(path, 32_000_000)
        for stage, pattern in (
            (output, "coordinator*.json"),
            (output / "comparison/semantic-review", "validation*.json"),
        ):
            for folder in ("requests", "responses", "measurements"):
                for path in sorted((stage / folder).glob(pattern)):
                    artifacts[str(path.relative_to(output))] = read_json(
                        path, 32_000_000
                    )
        if (output / "validation.json").exists():
            artifacts["validation.json"] = read_json(output / "validation.json")
        for name in (
            "paired-evidence-review.json",
            "paired-review-failure.json",
            "comparison-budget.json",
            "comparison/budget.json",
        ):
            if (output / name).exists():
                artifacts[name] = read_json(output / name)
        for path in sorted(output.glob("*-check.json")):
            artifacts[path.name] = read_json(path)
        receipt = output / "comparison/evidence.json"
        if (output / "paired-checks.json").exists():
            receipt = output / "paired-checks.json"
        summary = "Investigation recorded; no behavioral improvement is established."
        if receipt.exists():
            value = read_json(receipt)
            if value["status"] not in (
                "inconclusive",
                "regressed",
                "improvement_on_development_cases",
            ):
                raise ValueError("Invalid comparison status")
            summary = f"Comparison verdict: **{value['status']}**. Full task-specific reasoning and results are in the private comparison receipt."
            if value.get("assessment", {}).get("claim") == "mechanism_tested":
                summary += " This is executed mechanism-test evidence, not a complete historical-cohort or population-quality evaluation."
            for key in ("baseline", "candidate"):
                if value.get(key) and re.fullmatch(r"[a-f0-9]{40}", value[key]):
                    summary += f"\n\n{key.capitalize()}: `{value[key]}`."
            artifacts["comparison-receipt.json"] = value
        elif (output / "comparison-failure.json").exists():
            artifacts["comparison-failure.json"] = read_json(
                output / "comparison-failure.json"
            )
        request_path = output / "comparison-request.json"
        request = read_json(request_path) if request_path.exists() else {"plan": {}}
        if request_path.exists():
            artifacts["comparison-request.json"] = request
        runs = Path(request["plan"].get("recorded_runs") or output / "comparison/runs")
        if runs.exists():
            for path in sorted(runs.glob("*/*.json")):
                if path.name in (
                    "case.json",
                    "world.json",
                    "events.json",
                    "prompt.json",
                    "result.json",
                    "measurements.json",
                    "publication.json",
                ):
                    artifacts[str(path.relative_to(runs))] = read_json(path, 32_000_000)
            for path in (
                output / "comparison/runs/manifest.json",
                output / "comparison/runs/comparison.json",
                output / "comparison/semantic-assessment.json",
            ):
                if path.exists():
                    artifacts[path.name] = read_json(path, 32_000_000)
        for log in sorted(output.glob("*.log")):
            if log.stat().st_size > 16_000_000:
                raise ValueError("Execution log exceeds its byte budget")
            artifacts[log.name] = log.read_text(errors="replace")
        if publication:
            artifacts["publication.json"] = publication
            url = publication.get("pr_url", "")
            if re.fullmatch(
                r"https://github.com/"
                + re.escape(self.repository)
                + r"/pull/[1-9][0-9]*",
                url,
            ):
                summary += f"\n\nRepair PR: {url}."
        return self.record(
            "Attempts and decisions — " + output.name, summary, artifacts
        )


class IssueReader:
    def __init__(self, reader, issue, output):
        self.reader, self.issue, self.output, self.calls = reader, issue, output, 0
        self.schemas = [
            *reader.schemas,
            schema(
                "read_issue",
                "Read a named section of the central repair issue and discover prior attempt catalogs",
                {
                    "section": {"type": "string", "enum": ["all", *READ_SECTIONS]},
                    "offset": PAGE,
                },
                ["section", "offset"],
            ),
            schema(
                "read_issue_comment",
                "Read a catalog or discussion belonging to the central repair issue",
                {"comment_id": {"type": "integer", "minimum": 1}, "offset": PAGE},
                ["comment_id", "offset"],
            ),
            schema(
                "read_issue_artifact",
                "Read the complete private task, source, attempted patch, replay prompt, tool events or outcome referenced by an issue catalog",
                {
                    "reference": {"type": "string", "pattern": "^[a-f0-9]{64}\\.json$"},
                    "offset": PAGE,
                },
                ["reference", "offset"],
            ),
        ]

    def execute(self, name, arguments):
        if name not in ("read_issue", "read_issue_comment", "read_issue_artifact"):
            return self.reader.execute(name, arguments)
        field = {
            "read_issue": "section",
            "read_issue_comment": "comment_id",
            "read_issue_artifact": "reference",
        }[name]
        if (
            set(arguments) != {field, "offset"}
            or type(arguments["offset"]) is not int
            or not 0 <= arguments["offset"] <= 32_000_000
        ):
            raise ValueError("Invalid issue evidence request")
        records = []
        if name == "read_issue_artifact":
            text = self.issue.read_artifact(arguments["reference"])
        elif name == "read_issue":
            self.issue.refresh()
            text, section = self.issue.value["body"], arguments["section"]
            if section != "all":
                if section not in READ_SECTIONS:
                    raise ValueError("Unknown issue section")
                match = re.search(r"(?m)^## " + re.escape(section) + r"\n", text)
                if not match:
                    raise ValueError("Issue section is missing")
                end = re.search(r"(?m)^## ", text[match.end() :])
                text = text[
                    match.start() : match.end() + end.start() if end else len(text)
                ]
            records = self.issue.context()["records"]
        else:
            identifier = arguments["comment_id"]
            if type(identifier) is not int or identifier < 1:
                raise ValueError("Invalid issue comment")
            if self.issue.preview:
                value = next(
                    (item for item in self.issue.comments if item["id"] == identifier),
                    None,
                )
                if not value:
                    raise ValueError("Issue comment is missing")
            else:
                value = json.loads(
                    command(
                        [
                            "gh",
                            "api",
                            f"repos/{self.issue.repository}/issues/comments/{identifier}",
                        ]
                    )
                )
                if (
                    value["issue_url"]
                    != "https://api.github.com/" + self.issue.endpoint
                ):
                    raise ValueError("Comment is outside this repair issue")
            text = value["body"]
        offset = arguments["offset"]
        result = redact(
            {
                "content": text[offset : offset + 12000],
                "total_chars": len(text),
                "next_offset": offset + 12000 if len(text) > offset + 12000 else None,
                "records": records,
            }
        )
        self.calls += 1
        write_json(
            self.output / f"issue-read-{self.calls:03d}.json",
            {"tool": name, "arguments": arguments, "result": result},
        )
        return result

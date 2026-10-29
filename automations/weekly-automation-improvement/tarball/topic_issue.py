import hashlib
import json
import re
from pathlib import Path

from common import read_json, redact, write_json
from patches import command, github_pages, IMPROVEMENT_LABEL
from raw_evidence import schema, PAGE

SECTIONS = (
    "Goal",
    "Problem and impact",
    "Scope and source",
    "Evidence",
    "Validation",
    "Attempts and decisions",
)
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
    texts = (
        "Investigate this topic and propose one focused repair only when current-source evidence supports it. Record the diagnosis, uncertainty and attempts here.",
        f"Investigate **{topic['key'].replace('-', ' ')}**. Supporting observations and impact assessments are in the private evidence records linked below. Signals and rankings are unverified leads, not established causes or failure rates.",
        f"Automation: `{source['automation_ids'][0]}`.\n\n[Initial source baseline `{source['revision']}`](https://github.com/{source['repository']}/tree/{source['revision']}). Each frozen-context catalog records its own source revision. The configured revision is not proven to be the historically executed revision. Preserve the approved file scope; do not change unrelated automations or the evaluator.",
        "\n".join(links)
        + f"\n\nPrivate replay evidence stays in `{evidence_root}` on the automation worker. Read catalogued files using `read_issue_artifact(reference, offset)`; original production data is available through the Laminar tools. "
        + (f"Worker access: {location}. " if location else "")
        + "Do not publish raw tasks, prompts or logs in this issue. Missing evidence must remain explicit.\n\n<!-- topic-evidence:start -->\n<!-- topic-evidence:end -->",
        f"Each attempt uses the N recorded in its frozen-context catalog (first recorded N={snapshot.get('validation_cohort', {}).get('sample_size', 5)}): up to N topic-linked negative inputs and N random completed no-finding inputs, without replacing failures. Demonstrate the target fix and no new control violations using paired source versions. Preserve human content and required deliverables. Separate mechanism evidence, runtime/context gaps and general quality claims. A no-finding input is not assumed healthy.",
        "Each investigation and comparison has an immutable evidence catalog below. Private records include original tasks, source, attempted patches, prompts, tool events, final state and results. Consult prior attempts before proposing a repair. Unknown or non-reproducing outcomes do not prove improvement; keep an unqualified repair pending or draft.\n\n<!-- topic-attempts:start -->\n<!-- topic-attempts:end -->",
    )
    return f"<!-- automation-improvement-topic: {topic['id']} -->\n\n" + "\n\n".join(
        f"## {section}\n\n{text}" for section, text in zip(SECTIONS, texts, strict=True)
    )


class TopicIssue:
    def __init__(
        self, repository, output, evidence_root, value, comments, preview=False
    ):
        self.repository, self.output, self.evidence_root = (
            repository,
            output,
            evidence_root,
        )
        self.value, self.comments, self.preview = value, comments, preview
        self.endpoint = f"repos/{repository}/issues/{value['number']}"
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
                    "title": "Automation improvement: "
                    + topic["key"].replace("-", " "),
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
        return self.context()

    def context(self):
        return {
            "url": self.value["html_url"],
            "number": self.value["number"],
            "body": self.value["body"],
            "sections": list(SECTIONS),
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
        content = (
            f"## {title}\n\n{summary}\n\n### Private evidence catalog\n\nRead each file with `read_issue_artifact(reference, offset)`. Raw content stays on the automation worker.\n\n| Evidence | Reference |\n|---|---|\n"
            + "\n".join(
                f"| {item['name']} | `{item['reference']}` |" for item in catalog
            )
        )
        if len(content) > 60000:
            raise ValueError("Issue attempt catalog exceeds its size budget")
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
        self.refresh()
        start, end = f"<!-- topic-{kind}:start -->", f"<!-- topic-{kind}:end -->"
        body = self.value["body"]
        if (
            body.count(start) != 1
            or body.count(end) != 1
            or body.index(start) >= body.index(end)
        ):
            raise ValueError("Central issue record section is missing or ambiguous")
        link = f"- [{title}]({item.get('html_url') or '#preview'}) — comment `{item['id']}`.\n"
        if link not in body[body.index(start) : body.index(end)]:
            body = body.replace(end, link + end, 1)
            if self.preview:
                self.value["body"] = body
            else:
                self.sequence += 1
                payload = self.output / f"issue-body-{self.sequence:04d}.json"
                write_json(payload, {"body": body})
                self.value = json.loads(
                    command(
                        [
                            "gh",
                            "api",
                            "--method",
                            "PATCH",
                            self.endpoint,
                            "--input",
                            str(payload),
                        ]
                    )
                )
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
        receipt = output / "comparison/evidence.json"
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
        log = output / "comparison-runner.log"
        if log.exists():
            if log.stat().st_size > 16_000_000:
                raise ValueError("Comparison log exceeds its byte budget")
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
                    "section": {"type": "string", "enum": ["all", *SECTIONS]},
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
                if section not in SECTIONS:
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

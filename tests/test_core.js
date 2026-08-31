"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const Core = require("../fables-core.js");

const fixtures = path.join(__dirname, "fixtures");
const read = (name) => fs.readFileSync(path.join(fixtures, name), "utf8");

test("Claude parsing pairs tools and reports malformed or unknown records", () => {
  const parsed = Core.parseSession(read("claude.jsonl"), "claude", "claude");
  assert.equal(parsed.meta.title, "Fixture Claude session");
  assert.deepEqual([...parsed.meta.models], ["claude-fixture"]);
  assert.deepEqual([...parsed.meta.efforts], ["high"]);
  assert.equal(parsed.items.filter((item) => item.kind === "user").length, 1);
  assert.equal(parsed.items.find((item) => item.kind === "tool").output, "fixture contents");
  assert.equal(parsed.meta.diagnostics.malformedLines, 1);
  assert.equal(parsed.meta.diagnostics.unsupported["claude.future-record"], 1);
});

test("pi parsing pairs tool calls with toolResult records and tracks models", () => {
  const parsed = Core.parseSession(read("pi.jsonl"), "pi", "pi");
  assert.equal(parsed.meta.title, "Please inspect the fixture.");
  assert.equal(parsed.meta.cwd, "/Users/example/code/demo");
  assert.deepEqual([...parsed.meta.models], ["claude-fixture"]);
  assert.deepEqual([...parsed.meta.efforts], ["high"]);
  const readTool = parsed.items.find((item) => item.kind === "tool" && item.name === "read");
  assert.equal(readTool.output, "fixture contents");
  const bash = parsed.items.find((item) => item.kind === "tool" && item.name === "bash");
  assert.equal(bash.input, "ls demo");
  assert.equal(bash.output, "file.txt");
  assert.equal(parsed.meta.diagnostics.orphanCalls, 0);
  assert.equal(parsed.meta.diagnostics.orphanResults, 0);
  assert.equal(parsed.meta.diagnostics.unsupported["pi.custom"], 1);
});

test("Codex parsing preserves settings, cumulative tokens, and tool results", () => {
  const parsed = Core.parseSession(read("codex.jsonl"), "codex", "codex");
  assert.equal(parsed.meta.title, "Fixture Codex session");
  assert.equal(parsed.meta.tokens.input, 20);
  assert.equal(parsed.meta.tokens.cacheRead, 10);
  assert.equal(parsed.meta.tokens.reasoning, 3);
  assert.equal(parsed.items.find((item) => item.kind === "tool").output, "fixture");
  assert.equal(parsed.meta.diagnostics.orphanCalls, 0);
});

test("Copilot event parsing renders reasoning and execution events", () => {
  const parsed = Core.parseSession(read("copilot.jsonl"), "copilot", "copilot");
  assert.equal(parsed.meta.title, "Test Copilot parsing.");
  assert.equal(parsed.meta.cwd, "/Users/example/code/copilot");
  assert.equal(parsed.meta.tokens.input, 20);
  assert.equal(parsed.items.find((item) => item.kind === "thinking").text, "Need a tool.");
  assert.equal(parsed.items.find((item) => item.kind === "tool").output, "safe");
});

test("Gemini, OpenCode, and Cursor normalize to the same item kinds", () => {
  const cases = [
    ["gemini.json", "gemini"],
    ["opencode.json", "opencode"],
    ["cursor.json", "cursor"],
  ];
  for (const [name, format] of cases) {
    const parsed = Core.parseSession(read(name), format, format);
    const kinds = new Set(parsed.items.map((item) => item.kind));
    assert.ok(kinds.has("user"), format);
    assert.ok(kinds.has("assistant"), format);
    assert.ok(kinds.has("thinking"), format);
    assert.ok(parsed.meta.title.includes("Fixture") || parsed.meta.title.includes("Test"), format);
  }
  const gemini = Core.parseSession(read("gemini.json"), "gemini", "gemini");
  const opencode = Core.parseSession(read("opencode.json"), "opencode", "opencode");
  assert.ok(gemini.items.some((item) => item.kind === "tool"));
  assert.ok(opencode.items.some((item) => item.kind === "tool"));
});

test("Command Code records render reasoning, tools, and paired results", () => {
  const parsed = Core.parseSession(read("commandcode.jsonl"), "commandcode", "commandcode");
  assert.equal(parsed.meta.format, "commandcode");
  assert.equal(parsed.meta.title, "Check the pricing page.");
  assert.ok(parsed.items.some((item) => item.kind === "thinking" &&
    item.text.includes("Need to fetch")));
  const tool = parsed.items.find((item) => item.kind === "tool");
  assert.equal(tool.name, "WebFetch");
  assert.equal(tool.output, "$10/month");
  assert.equal(parsed.meta.diagnostics.orphanResults, 0);
});

test("Prime Agent sessions parse through the pi schema", () => {
  const parsed = Core.parseSession(read("prime.jsonl"), "prime", "prime");
  assert.equal(parsed.meta.source, "prime");
  assert.equal(parsed.meta.cwd, "/Users/example/code/demo");
  assert.deepEqual([...parsed.meta.models], ["gpt-fixture"]);
  const tool = parsed.items.find((item) => item.kind === "tool");
  assert.equal(tool.name, "bash");
  assert.equal(tool.output, "file.txt");
});

test("Qwen, Aider, and Kiro transcripts render", () => {
  const qwen = Core.parseSession(
    JSON.stringify({
      uuid: "u1", role: "user", content: [{"type": "text", "text": "Qwen question"}],
    }) + "\n" + JSON.stringify({
      uuid: "a1", role: "assistant", content: [{"type": "text", "text": "Qwen answer"}],
    }), "qwen", "qwen");
  assert.equal(qwen.meta.source, "qwen");
  assert.equal(qwen.items.find((item) => item.kind === "user").text, "Qwen question");

  const aider = Core.parseSession("# aider chat started at 2024-12-03 17:45:38\n\n>>> user\nfix it\n", "aider", "aider");
  assert.equal(aider.meta.source, "aider");
  assert.ok(aider.items.some((item) => item.kind === "info" && item.text.includes("fix it")));

  const kiro = Core.parseSession(
    JSON.stringify({type: "user_message", content: "Kiro question"}) + "\n" +
    JSON.stringify({type: "agent_message", content: "Kiro answer"}), "kiro", "kiro");
  assert.equal(kiro.meta.source, "kiro");
  assert.ok(kiro.items.some((item) => item.kind === "user" && item.text === "Kiro question"));
  assert.ok(kiro.items.some((item) => item.kind === "assistant" && item.text === "Kiro answer"));
});

test("Kimi archives render thinking, tool names from wire, and results", () => {
  const parsed = Core.parseSession(read("kimi-archive.json"), "kimi", "kimi");
  assert.equal(parsed.meta.format, "kimi");
  assert.equal(parsed.meta.title, "What files are here?");
  assert.ok(parsed.items.some((item) => item.kind === "thinking" &&
    item.text.includes("Need a tool")));
  const tool = parsed.items.find((item) => item.kind === "tool");
  assert.equal(tool.name, "Shell");
  assert.equal(tool.output, "file.txt");
  assert.equal(parsed.items.find((item) => item.kind === "user").text,
    "What files are here?");
});

test("Cursor CLI role-based transcripts preserve tool calls", () => {
  const parsed = Core.parseSession(read("cursor-cli.jsonl"), "cursor-cli", "cursor-cli");
  assert.equal(parsed.meta.format, "cursor-cli");
  assert.equal(parsed.meta.title, "Inspect the fixture.");
  const kinds = new Set(parsed.items.map((item) => item.kind));
  assert.ok(kinds.has("user") && kinds.has("assistant") && kinds.has("tool"));
  const shell = parsed.items.find((item) => item.kind === "tool" && item.name === "Shell");
  assert.deepEqual(shell.input, { command: "ls demo" });
  const readTool = parsed.items.find((item) => item.kind === "tool" && item.name === "Read");
  assert.equal(readTool.output, null);  // transcripts carry no results
});

test("Cline, Goose, and VS Code preserve messages, tools, and metadata", () => {
  const cases = [
    ["cline.json", "cline"],
    ["goose.json", "goose"],
    ["vscode.json", "vscode"],
  ];
  for (const [name, format] of cases) {
    const parsed = Core.parseSession(read(name), format, format);
    const kinds = new Set(parsed.items.map((item) => item.kind));
    assert.ok(kinds.has("user"), format);
    assert.ok(kinds.has("assistant"), format);
    assert.ok(kinds.has("thinking"), format);
    assert.ok(kinds.has("tool"), format);
    assert.ok(parsed.meta.title.includes("Fixture"), format);
    assert.ok(parsed.meta.cwd.includes("/code/"), format);
    assert.equal(parsed.meta.diagnostics.orphanCalls, 0, format);
  }
  const vscode = Core.parseSession(read("vscode.json"), "vscode", "vscode");
  assert.equal(vscode.meta.diagnostics.malformedLines, 1);
  assert.equal(vscode.meta.tokens.reasoning, 2);
});

test("standalone archives exclude hidden categories and redact sensitive text", () => {
  const parsed = Core.parseSession(read("claude.jsonl"), "claude", "claude");
  parsed.items.push({
    kind: "assistant",
    text: "token sk-ant-abcdefghijklmnop at /Users/private/code and me@example.com",
    raw: [{ secret: "sk-ant-abcdefghijklmnop" }],
  });
  const findings = Core.inspectSensitive(parsed);
  assert.ok(findings.secrets >= 1);
  assert.ok(findings.paths >= 1);
  assert.ok(findings.emails >= 1);

  const archive = Core.makeShareArchive(parsed, {
    includeThinking: false,
    includeTools: false,
    includeInfo: false,
    includeRaw: false,
    redactSecrets: true,
    redactPaths: true,
    redactEmails: true,
  });
  assert.ok(!archive.items.some((item) => item.kind === "thinking"));
  assert.ok(!archive.items.some((item) => item.kind === "tool"));
  assert.ok(archive.items.every((item) => item.raw === undefined));
  const serialized = JSON.stringify(archive);
  assert.ok(!serialized.includes("sk-ant-abcdefghijklmnop"));
  assert.ok(!serialized.includes("/Users/private"));
  assert.ok(!serialized.includes("me@example.com"));

  const roundTrip = Core.parseArchive(serialized);
  assert.equal(roundTrip.meta.source, "claude");
  assert.equal(roundTrip.items.length, archive.items.length);
});

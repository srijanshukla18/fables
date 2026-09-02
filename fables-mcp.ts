/**
 * Fables MCP bridge for pi.
 *
 * pi has no built-in MCP client, so this extension spawns the Fables MCP
 * server (fables-mcp.py) as a child process and registers its tools —
 * list_sessions, get_session, search_sessions — as native pi tools, using
 * the stateless MCP protocol (2026-07-28): newline-delimited JSON-RPC over
 * stdio, no handshake, protocol version carried per request in `_meta`.
 *
 * The server command is resolved in this order:
 *   1. ~/.pi/agent/extensions/fables-mcp.json  (written by install-mcp.py)
 *   2. $FABLES_MCP_CMD / $FABLES_MCP_ARGS (comma-separated)
 *   3. `uv run <script>` where <script> is FABLES_INSTALL_DIR/fables-mcp.py,
 *      ~/.local/share/fables/fables-mcp.py, or this file's directory
 *   4. `python3 <script>` with the same candidate paths
 *
 * Install: run install-mcp.py, or copy this file to ~/.pi/agent/extensions/
 * and restart pi.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { Type } from "typebox";

const PROTOCOL_VERSION = "2026-07-28";
const PREFIX = "fables_";
const REQUEST_TIMEOUT_MS = 30_000;
const CONNECT_TIMEOUTS_MS = [3_000, 10_000] as const;
const CONNECT_RETRY_DELAY_MS = 150;

interface ServerConfig {
	cmd: string;
	args: string[];
}

function serverConfig(): ServerConfig | null {
	// 1. Sidecar written by install-mcp.py.
	const sidecar = join(homedir(), ".pi", "agent", "extensions", "fables-mcp.json");
	try {
		const value = JSON.parse(readFileSync(sidecar, "utf8")) as ServerConfig;
		if (value && typeof value.cmd === "string") return { cmd: value.cmd, args: Array.isArray(value.args) ? value.args.map(String) : [] };
	} catch {
		// fall through
	}
	// 2. Environment override.
	if (process.env.FABLES_MCP_CMD) {
		return {
			cmd: process.env.FABLES_MCP_CMD,
			args: (process.env.FABLES_MCP_ARGS || "").split(",").filter(Boolean),
		};
	}
	// 3/4. uv / python3 with the usual script locations.
	const candidates: string[] = [];
	if (process.env.FABLES_INSTALL_DIR) candidates.push(join(process.env.FABLES_INSTALL_DIR, "fables-mcp.py"));
	candidates.push(join(homedir(), ".local", "share", "fables", "fables-mcp.py"));
	candidates.push(join(__dirname, "fables-mcp.py"));
	for (const script of candidates) {
		if (existsSync(script)) {
			return { cmd: "uv", args: ["run", script] };
		}
	}
	return null;
}

interface PendingRequest {
	resolve: (value: unknown) => void;
	reject: (reason: Error) => void;
	timer: NodeJS.Timeout;
}

class McpClient {
	private child: ChildProcessWithoutNullStreams | null = null;
	private pending = new Map<number, PendingRequest>();
	private buffer = "";
	private nextId = 1;
	private connectPromise: Promise<boolean> | null = null;

	constructor(private config: ServerConfig) {}

	private attach(process: ChildProcessWithoutNullStreams) {
		process.stdout.setEncoding("utf8");
		process.stdout.on("data", (chunk: string) => {
			if (this.child === process) this.onData(chunk);
		});
		process.stderr.on("data", () => { /* server logs */ });
		process.on("error", (error) => {
			if (this.child === process) this.failAll(new Error(`fables-mcp failed to start: ${error.message}`));
		});
		process.on("exit", () => {
			if (this.child === process) this.failAll(new Error("fables-mcp exited"));
		});
		// Detach from the event loop: without this, the child's stdio pipes keep pi
		// alive after it would otherwise exit (headless --print never terminates).
		// The server exits on its own once stdin closes.
		process.unref?.();
		process.stdin.unref?.();
		process.stdout.unref?.();
		process.stderr.unref?.();
	}

	private onData(chunk: string) {
		this.buffer += chunk;
		let newline: number;
		while ((newline = this.buffer.indexOf("\n")) >= 0) {
			const line = this.buffer.slice(0, newline);
			this.buffer = this.buffer.slice(newline + 1);
			if (!line.trim()) continue;
			try {
				const message = JSON.parse(line) as { id?: number; result?: { isError?: boolean; content?: Array<{ text?: string }> }; error?: { message?: string } };
				if (message.id === undefined) continue; // notification
				const request = this.pending.get(message.id);
				if (!request) continue;
				this.pending.delete(message.id);
				clearTimeout(request.timer);
				if (message.error) request.reject(new Error(message.error.message || "fables-mcp error"));
				else request.resolve(message.result ?? {});
			} catch {
				// Ignore malformed lines from the server.
			}
		}
	}

	private failAll(error: Error) {
		for (const request of this.pending.values()) {
			clearTimeout(request.timer);
			request.reject(error);
		}
		this.pending.clear();
		this.buffer = "";
		this.child = null;
	}

	private stopChild(): void {
		const process = this.child;
		this.child = null;
		this.buffer = "";
		if (process) {
			try { process.kill(); } catch { /* already gone */ }
		}
	}

	kill(): void {
		this.stopChild();
		for (const request of this.pending.values()) {
			clearTimeout(request.timer);
			request.reject(new Error("fables-mcp session ended"));
		}
		this.pending.clear();
	}

	private async connectWithRetry(): Promise<boolean> {
		let lastError: Error | undefined;
		for (let attempt = 0; attempt < CONNECT_TIMEOUTS_MS.length; attempt++) {
			this.stopChild();
			try {
				const process = spawn(this.config.cmd, this.config.args, { stdio: ["pipe", "pipe", "pipe"] });
				this.attach(process);
				this.child = process;
				await this.request("server/discover", {}, CONNECT_TIMEOUTS_MS[attempt]);
				return true;
			} catch (error) {
				lastError = error as Error;
				this.stopChild();
				if (attempt + 1 < CONNECT_TIMEOUTS_MS.length) {
					await new Promise((resolve) => setTimeout(resolve, CONNECT_RETRY_DELAY_MS));
				}
			}
		}
		console.error(`fables-mcp: server unusable after retry: ${lastError?.message ?? "unknown error"}`);
		return false;
	}

	async ensureConnected(): Promise<boolean> {
		if (this.child && this.child.exitCode === null) return true;
		if (this.connectPromise) return this.connectPromise;
		const promise = this.connectWithRetry();
		this.connectPromise = promise;
		void promise.finally(() => {
			if (this.connectPromise === promise) this.connectPromise = null;
		});
		return promise;
	}

	request(method: string, params: Record<string, unknown>, timeoutMs = REQUEST_TIMEOUT_MS): Promise<unknown> {
		return new Promise((resolve, reject) => {
			if (!this.child || this.child.exitCode !== null) {
				reject(new Error("fables-mcp is not connected"));
				return;
			}
			const id = this.nextId++;
			const timer = setTimeout(() => {
				this.pending.delete(id);
				reject(new Error(`fables-mcp ${method} timed out`));
			}, timeoutMs);
			this.pending.set(id, { resolve, reject, timer });
			const message = {
				jsonrpc: "2.0",
				id,
				method,
				params: {
					...params,
					_meta: {
						"io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
						"io.modelcontextprotocol/clientCapabilities": {},
					},
				},
			};
			try {
				this.child.stdin.write(JSON.stringify(message) + "\n");
			} catch (error) {
				clearTimeout(timer);
				this.pending.delete(id);
				reject(error as Error);
			}
		});
	}

	async listTools(): Promise<Array<{ name: string; description?: string; inputSchema?: unknown }>> {
		const result = (await this.request("tools/list", {})) as { tools?: Array<{ name: string; description?: string; inputSchema?: unknown }> };
		return result.tools ?? [];
	}

	async callTool(name: string, argumentsValue: Record<string, unknown>): Promise<string> {
		const result = (await this.request("tools/call", { name, arguments: argumentsValue })) as {
			isError?: boolean;
			content?: Array<{ type?: string; text?: string }>;
		};
		if (result.isError) {
			throw new Error(result.content?.[0]?.text || `fables tool ${name} failed`);
		}
		return result.content?.[0]?.text ?? "";
	}
}

function schemaToTypebox(schema: unknown): unknown {
	const value = (schema ?? {}) as { properties?: Record<string, { type?: string; description?: string }>; required?: string[] };
	const properties: Record<string, unknown> = {};
	const required = value.required ?? [];
	for (const [key, def] of Object.entries(value.properties ?? {})) {
		let type: unknown;
		if (def.type === "integer" || def.type === "number") type = Type.Number({ description: def.description });
		else if (def.type === "boolean") type = Type.Boolean({ description: def.description });
		else type = Type.String({ description: def.description });
		properties[key] = required.includes(key) ? type : Type.Optional(type);
	}
	return Type.Object(properties);
}

export default function fablesMcpExtension(pi: ExtensionAPI) {
	const config = serverConfig();
	if (!config) {
		console.error("fables-mcp: no server configuration found — run install-mcp.py");
		return;
	}
	const client = new McpClient(config);

	pi.on("session_shutdown", async () => {
		client.kill();
	});

	pi.on("session_start", async (_event, ctx) => {
		const connected = await client.ensureConnected();
		if (!connected) {
			ctx.ui.notify("fables-mcp: server unavailable", "error");
			return;
		}
		let tools: Array<{ name: string; description?: string; inputSchema?: unknown }> = [];
		try {
			tools = await client.listTools();
		} catch (error) {
			ctx.ui.notify(`fables-mcp: tools/list failed: ${(error as Error).message}`, "error");
			return;
		}
		let registered = 0;
		for (const tool of tools) {
			const toolName = PREFIX + tool.name;
			pi.registerTool({
				name: toolName,
				label: `Fables · ${tool.name}`,
				description: tool.description || `Fables MCP tool: ${tool.name}`,
				promptSnippet: `Use ${toolName} to query local agent session archives (${tool.name}).`,
				promptGuidelines: [
					`Prefer ${toolName} when the user wants to find, fetch, or search past coding-agent conversations.`,
				],
				parameters: schemaToTypebox(tool.inputSchema) as never,
				async execute(_toolCallId, params) {
					if (!(await client.ensureConnected())) {
						throw new Error("fables-mcp server is not available");
					}
					const text = await client.callTool(tool.name, (params ?? {}) as Record<string, unknown>);
					return { content: [{ type: "text", text }] };
				},
			});
			registered++;
		}
		if (registered === 0) {
			ctx.ui.notify("fables-mcp: no tools advertised", "info");
		}
	});
}

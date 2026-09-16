import { HouseJob } from "@masonrhodesdev/pulumi-home-lab";
import { nomad } from "./ci";

/**
 * llm — a small local model for pm-loop's PROSE ONLY (STATUS sentences, PR-body summaries,
 * CI-log triage lines). Never verdicts, never merges. OpenAI-compatible HTTP on the host's :8080
 * (HouseJob = host networking), LAN-only: pm-loop reads `llm.endpoint = "http://192.168.1.3:8080"`.
 *
 * CPU inference, pinned to node-3 (i7-6700T, 8 threads, 15 GiB): Qwen2.5-7B-Instruct Q4_K_M runs
 * at roughly 5–8 tok/s there — fine for a 60-word sentence a few times an hour. Swap MODEL_URL to
 * the 3B GGUF if latency matters more than quality. The GGUF (~4.7 GB) is fetched once into the
 * allocation's ephemeral disk; a reschedule refetches it (HouseJob does not expose sticky disks).
 * The 8 GiB reservation is a hard one (oversubscription is off) and node-3 has ~15 GiB.
 *
 * Copy to home-lab/.infra/llm.ts and add `export { llm } from "./llm";` to index.ts.
 */
const MODEL_URL = "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/qwen2.5-7b-instruct-q4_k_m.gguf";
const ALIAS = "qwen2.5-7b-instruct-q4_k_m";

export const llm = new HouseJob("llm", {
    name: "llm",
    type: "service",
    node: "node-3",
    ephemeralDiskMB: 8192,
    tasks: [{
        name: "server",
        image: "ghcr.io/ggml-org/llama.cpp:server",
        command: "/bin/sh",
        args: ["-c", [
            "set -e",
            "f=/alloc/data/model.gguf",
            `[ -s "$f" ] || { wget -qO "$f.part" '${MODEL_URL}' && mv "$f.part" "$f"; }`,
            `exec /app/llama-server -m "$f" --host 0.0.0.0 --port 8080 -c 4096 -t 6 --parallel 2 --alias ${ALIAS}`,
        ].join("; ")],
        resources: { cpu: 6000, memoryMB: 8192 },
    }],
}, { providers: [nomad] });

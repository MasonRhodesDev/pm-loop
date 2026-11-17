import { HouseJob } from "@masonrhodesdev/pulumi-home-lab";
import { nomad } from "./ci";

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

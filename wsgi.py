
# --- Configuration ---


from flask import Flask, request, jsonify
import subprocess
import os
import json
import uuid
from openai import OpenAI
import re

app = Flask(__name__)

client = OpenAI(api_key="*****")
PROJECTS_ROOT = "./generated_projects"
CACHE_FILE = "terminal_cache.json"


# --- Utils ---
def call_openai_response(prompt, model="gpt-4.1"):
    print("[Instruction] Calling OpenAI with prompt:", prompt)
    response = client.responses.create(
        model=model,
        input=prompt,
        store=True,
        parallel_tool_calls=True,
        text={"format": {"type": "text"}}
    )
    output = response.output[0].content[0].text.strip()
    print("[Instruction] OpenAI response:", output)
    return output

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)

# --- Agent: Instruction Agent ---
def instruction_agent(user_prompt):
    print("[Instruction Agent] Received user prompt:", user_prompt)
    template = f"""
You are an Instruction Agent. Break the following task into two instructions:
1. One for a Developer Agent to write the necessary Python code.
2. One for a Terminal Agent to create directory, save the code, and run the app.

TASK: {user_prompt}

Respond in JSON with `dev_instruction` and `terminal_instruction`.
"""
    result = call_openai_response(template)
    return json.loads(result)

# --- Agent: Developer Agent ---
def developer_agent(dev_instruction):
    print("[Developer Agent] Instruction:", dev_instruction)
    prompt = f"Dev Agent: {dev_instruction}\nReturn only the Python code in a code block."
    result = call_openai_response(prompt)
    code = result.split("```python")[-1].split("```", 1)[0].strip()
    print("[Developer Agent] Generated code:\n", code)
    return code

# --- Agent: Debugger Agent ---
# --- Agent: Debugger Agent ---
def debugger_agent(error_message, last_code):
    print("[Debugger Agent] Handling error:", error_message)

    if any(key in error_message for key in ["arguments are required", "Usage:", "Please enter your API key"]):
        print("[Debugger Agent] Human input required for CLI args or ENV.")
        user_inputs = {}
        if "city" in error_message.lower():
            user_inputs["CITY_NAME"] = input("Please enter the city name: ")
        if "api key" in error_message.lower():
            user_inputs["API_KEY"] = input("Please enter your OpenWeatherMap API key: ")
        return "rerun", user_inputs

    if any(keyword in error_message for keyword in ["ModuleNotFoundError", "ImportError", "Permission denied", "command not found"]):
        fix_prompt = f"You received this system error while running a script: {error_message}\nGenerate terminal commands to fix it."
        return "terminal", call_openai_response(fix_prompt)

    fix_prompt = f"You wrote this code:\n```python\n{last_code}\n```\nIt produced this error:\n{error_message}\nPlease fix it and return the corrected Python code only."
    fixed_code = call_openai_response(fix_prompt)
    fixed_code = fixed_code.split("```python")[-1].split("```", 1)[0].strip()
    return "developer", fixed_code
# --- Agent: Terminal Agent ---
def terminal_agent(project_id, terminal_instruction, code):
    print("[Terminal Agent] Task:", terminal_instruction)
    project_path = os.path.join(PROJECTS_ROOT, project_id)
    os.makedirs(project_path, exist_ok=True)
    main_file = os.path.join(project_path, "main.py")

    cache = load_cache()
    if project_id not in cache:
        cache[project_id] = {
            "steps": [
                {"step": "mkdir", "status": "done"},
                {"step": "write_file", "status": "pending"},
                {"step": "run_script", "status": "pending"},
            ],
            "code": code
        }
        save_cache(cache)

    steps = cache[project_id]["steps"]
    cache[project_id]["code"] = code

    if steps[1]["status"] == "pending":
        print(f"[Terminal Agent] Writing main.py to {main_file}")
        with open(main_file, "w") as f:
            f.write(code)
        steps[1]["status"] = "done"
        save_cache(cache)

    if steps[2]["status"] == "pending":
        try:
            print(f"[Terminal Agent] Executing: python3 {main_file}")
            process = subprocess.Popen(
                ["python3", "main.py"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=project_path,
                universal_newlines=True
            )
            output_log = ""
            print("[Terminal Agent] Process started. Output:")
            for line in process.stdout:
                print("[App Output]", line.strip())
                output_log += line
            process.wait()
            if "Please enter your" in output_log or "arguments are required" in output_log:
                raise RuntimeError(output_log)
            steps[2]["status"] = "done"
            save_cache(cache)
            return output_log

        except Exception as e:
            error_message = str(e)
            print("[Terminal Agent] Caught error:", error_message)
            last_code = cache[project_id].get("code", "")
            agent_type, result = debugger_agent(error_message, last_code)
            if agent_type == "terminal":
                print("[Debugger → Terminal Agent] Retrying with command:", result)
                os.system(result)
                return terminal_agent(project_id, terminal_instruction, last_code)
            elif agent_type == "developer":
                print("[Debugger → Developer Agent] Retrying with fixed code:")
                return terminal_agent(project_id, terminal_instruction, result)
            elif agent_type == "rerun":
                print("[Debugger → Terminal Agent] Re-running with human-provided args:")
                env = os.environ.copy()
                env.update(result)
                process = subprocess.Popen(
                    ["python3", "main.py"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=project_path,
                    universal_newlines=True,
                    env=env
                )
                output_log = ""
                for line in process.stdout:
                    print("[App Output]", line.strip())
                    output_log += line
                process.wait()
                steps[2]["status"] = "done"
                save_cache(cache)
                return output_log

    return "Already executed."


# --- Controller Endpoint ---
@app.route("/run", methods=["POST"])
def run_task():
    user_prompt = request.json.get("task")
    if not user_prompt:
        return jsonify({"error": "Missing 'task' in request body."}), 400

    print("[Controller] Received task:", user_prompt)
    project_id = str(uuid.uuid4())

    instructions = instruction_agent(user_prompt)
    dev_code = developer_agent(instructions["dev_instruction"])

    final_output = None
    while final_output is None or "Traceback" in final_output:
        final_output = terminal_agent(project_id, instructions["terminal_instruction"], dev_code)
        if "Traceback" in final_output:
            print("[Controller] Error detected, retrying...")

    return jsonify({
        "project_id": project_id,
        "dev_instruction": instructions["dev_instruction"],
        "terminal_instruction": instructions["terminal_instruction"],
        "code": dev_code,
        "output": final_output
    })

if __name__ == "__main__":
    os.makedirs(PROJECTS_ROOT, exist_ok=True)
    app.run(debug=True, port=5000)

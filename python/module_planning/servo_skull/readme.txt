Make sure faster-whisper, openai, numpy are installed.
espeak-ng must be in PATH.
text-generation-webui must be running with the OpenAI extension enabled.
Adjust SERIAL_PORT, LLM_BASE_URL and model name at the top.
Run: python servo_skull.py

pip install espeak-ng-python
pip install openai[aiohttp]
pip install Jinja2
FROM ollama/ollama:latest

# Start Ollama server in background and wait for it to become ready
RUN nohup ollama serve > /tmp/ollama.log 2>&1 & \
    sleep 10 && \
    ollama pull qwen2.5:3b && \
    ollama pull mxbai-embed-large:latest

# Expose Ollama port
EXPOSE 11434

# Run the Ollama server normally
CMD ["serve"]
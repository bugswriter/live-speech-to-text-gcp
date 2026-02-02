/**
 * AudioWorklet processor for capturing raw PCM audio
 * 
 * This runs in a separate audio thread and sends raw samples
 * to the main thread via postMessage.
 */
class AudioProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this.bufferSize = 4096; // Accumulate samples before sending
        this.buffer = new Float32Array(this.bufferSize);
        this.bufferIndex = 0;
    }

    process(inputs, outputs, parameters) {
        const input = inputs[0];
        if (!input || !input[0]) return true;

        const samples = input[0]; // Mono channel

        for (let i = 0; i < samples.length; i++) {
            this.buffer[this.bufferIndex++] = samples[i];

            if (this.bufferIndex >= this.bufferSize) {
                // Convert Float32 [-1, 1] to Int16 [-32768, 32767]
                const int16 = new Int16Array(this.bufferSize);
                for (let j = 0; j < this.bufferSize; j++) {
                    const s = Math.max(-1, Math.min(1, this.buffer[j]));
                    int16[j] = s < 0 ? s * 0x8000 : s * 0x7FFF;
                }

                // Send to main thread
                this.port.postMessage(int16.buffer, [int16.buffer]);

                // Reset buffer
                this.buffer = new Float32Array(this.bufferSize);
                this.bufferIndex = 0;
            }
        }

        return true;
    }
}

registerProcessor('audio-processor', AudioProcessor);

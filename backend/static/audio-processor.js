/**
 * AudioWorklet processor for capturing raw PCM audio and resampling
 * 
 * This runs in a separate audio thread and sends raw samples
 * to the main thread via postMessage. It handles resampling
 * to a target sample rate (e.g., 16kHz for Google Speech API).
 */
class AudioProcessor extends AudioWorkletProcessor {
    constructor(options) {
        super();
        this.bufferSize = 4096; // Accumulate samples before sending
        this.buffer = new Float32Array(this.bufferSize);
        this.bufferIndex = 0;

        // Get processor options
        const { targetSampleRate = 16000 } = options.processorOptions || {};
        this.targetSampleRate = targetSampleRate;

        // AudioContext's sample rate (this is constant for the worklet's lifetime)
        this.contextSampleRate = sampleRate; 

        // Resampler specific properties
        this.resampleRatio = this.targetSampleRate / this.contextSampleRate;
        this.resamplerBuffer = []; // Buffer for input samples to resampler
    }

    // Simple linear interpolation resampler
    _resample(inputBuffer) {
        if (this.resampleRatio === 1) {
            return inputBuffer; // No resampling needed
        }

        const outputLength = Math.floor(inputBuffer.length * this.resampleRatio);
        const outputBuffer = new Float32Array(outputLength);

        for (let i = 0; i < outputLength; i++) {
            const inputIndex = i / this.resampleRatio;
            const indexFloor = Math.floor(inputIndex);
            const indexCeil = Math.ceil(inputIndex);
            const frac = inputIndex - indexFloor;

            const v0 = inputBuffer[indexFloor] || 0;
            const v1 = inputBuffer[indexCeil] || 0;

            outputBuffer[i] = v0 + (v1 - v0) * frac;
        }
        return outputBuffer;
    }

    process(inputs, outputs, parameters) {
        const input = inputs[0];
        if (!input || !input[0]) return true;

        const samples = input[0]; // Mono channel

        // Add current samples to resampler buffer
        this.resamplerBuffer.push(...samples);

        // Process only when we have enough samples to potentially fill an output buffer
        // Or when the input stream ends (not explicitly handled here for simplicity,
        // but typically a flush mechanism would be needed for the last few samples)
        while (this.resamplerBuffer.length >= this.contextSampleRate / 10) { // Process in chunks
            const samplesToResampleCount = Math.min(this.resamplerBuffer.length, 2048); // Arbitrary chunk size
            const samplesToResample = this.resamplerBuffer.splice(0, samplesToResampleCount);
            const resampledSamples = this._resample(samplesToResample);

            for (let i = 0; i < resampledSamples.length; i++) {
                this.buffer[this.bufferIndex++] = resampledSamples[i];

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
        }
        return true;
    }
}

registerProcessor('audio-processor', AudioProcessor);

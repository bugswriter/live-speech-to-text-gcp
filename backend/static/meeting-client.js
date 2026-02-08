/**
 * WebRTC Audio Streaming Client
 * 
 * Captures raw PCM audio (Int16, 16kHz, mono) using AudioWorklet
 * and streams it over WebSocket to match Google Speech API format.
 */

class MeetingClient {
    constructor(meetingId, onStateUpdate, onInterimTranscript) {
        this.meetingId = meetingId;
        this.onStateUpdate = onStateUpdate;
        this.onInterimTranscript = onInterimTranscript;
        
        this.ws = null;
        this.audioContext = null;
        this.audioWorklet = null;
        this.audioStream = null;
        this.isRecording = false;
        this.meetingState = null;
    }

    async connect() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws/meeting/${this.meetingId}`;
        
        return new Promise((resolve, reject) => {
            this.ws = new WebSocket(wsUrl);
            
            this.ws.onopen = () => {
                console.log('WebSocket connected');
                resolve();
            };
            
            this.ws.onerror = (error) => {
                console.error('WebSocket error:', error);
                reject(error);
            };
            
            this.ws.onclose = () => {
                console.log('WebSocket disconnected');
                this.stopRecording();
            };
            
            this.ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                this._handleMessage(data);
            };
        });
    }

    _handleMessage(data) {
        switch (data.type) {
            case 'state_sync':
            case 'state_update':
                this.meetingState = data.meeting;
                if (this.onStateUpdate) {
                    this.onStateUpdate(data.meeting);
                }
                break;
                
            case 'interim_transcript':
                if (this.onInterimTranscript) {
                    this.onInterimTranscript(data.text, data.speaker);
                }
                break;
                
            case 'recording_started':
                console.log('Recording started on server');
                break;
                
            case 'recording_stopped':
                console.log('Recording stopped, final state:', data.meeting);
                this.meetingState = data.meeting;
                if (this.onStateUpdate) {
                    this.onStateUpdate(data.meeting);
                }
                break;
        }
    }

    async startRecording() {
        if (this.isRecording) return;
        
        try {
            // Get microphone access, requesting 16kHz but being flexible
            this.audioStream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    channelCount: 1,
                    sampleRate: 16000,  // Request 16kHz
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true,
                }
            });
            
            // Get the actual sample rate from the audio stream's settings
            const actualSampleRate = this.audioStream.getAudioTracks()[0].getSettings().sampleRate;
            console.log('Actual microphone sample rate:', actualSampleRate);

            // Create AudioContext with the *actual* sample rate of the microphone
            // We will resample to 16kHz in the AudioWorklet if needed.
            this.audioContext = new AudioContext({ sampleRate: actualSampleRate });
            
            // Load the AudioWorklet processor
            await this.audioContext.audioWorklet.addModule('/static/audio-processor.js');
            
            // Create source from microphone
            const source = this.audioContext.createMediaStreamSource(this.audioStream);
            
            // Create AudioWorklet node
            this.audioWorklet = new AudioWorkletNode(this.audioContext, 'audio-processor', {
                processorOptions: {
                    targetSampleRate: 16000 // Tell worklet to resample to this
                }
            });
            
            // Handle audio data from worklet
            this.audioWorklet.port.onmessage = (event) => {
                if (this.ws?.readyState === WebSocket.OPEN) {
                    // event.data is ArrayBuffer of Int16 samples (now guaranteed 16kHz)
                    this.ws.send(event.data);
                }
            };
            
            // Connect: microphone -> worklet
            source.connect(this.audioWorklet);
            // Note: We don't connect to destination (speakers) to avoid feedback
            
            this.isRecording = true;
            
            // Tell server to start streaming to Google
            this.ws.send(JSON.stringify({ type: 'start_recording' }));
            
            console.log('Recording started - sending raw PCM at 16kHz');
        } catch (error) {
            console.error('Failed to start recording:', error);
            throw error;
        }
    }

    stopRecording() {
        if (!this.isRecording) return;
        
        // Disconnect audio worklet
        if (this.audioWorklet) {
            this.audioWorklet.disconnect();
            this.audioWorklet = null;
        }
        
        // Close audio context
        if (this.audioContext) {
            this.audioContext.close();
            this.audioContext = null;
        }
        
        // Stop microphone
        if (this.audioStream) {
            this.audioStream.getTracks().forEach(track => track.stop());
            this.audioStream = null;
        }
        
        // Tell server to stop
        if (this.ws?.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ type: 'stop_recording' }));
        }
        
        this.isRecording = false;
        console.log('Recording stopped');
    }

    updateTitle(title) {
        if (this.ws?.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ type: 'update_title', title }));
        }
    }

    disconnect() {
        this.stopRecording();
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
    }
}


/**
 * Meeting Notes UI Renderer
 */
class MeetingNotesRenderer {
    constructor(containerId) {
        this.container = document.getElementById(containerId);
    }

    render(meeting) {
        if (!meeting) return;
        
        // Check if we have any content to show
        const hasContent = meeting.summary || 
                          meeting.key_points?.length > 0 || 
                          meeting.decisions?.length > 0 || 
                          meeting.action_items?.length > 0 ||
                          meeting.open_questions?.length > 0 ||
                          meeting.transcript?.length > 0;
        
        if (!hasContent) {
            this.container.innerHTML = `
                <div class="empty-state">
                    <h2>Ready to Record</h2>
                    <p>Click "Start Recording" to begin capturing the meeting. Notes will be generated automatically every 30 seconds.</p>
                </div>
            `;
            return;
        }
        
        this.container.innerHTML = `
            <div class="meeting-notes">
                ${meeting.summary ? `
                    <section class="notes-section">
                        <h2><span class="icon">*</span> Summary</h2>
                        <div class="summary-content">${this._formatMultiline(meeting.summary)}</div>
                    </section>
                ` : ''}
                
                ${meeting.key_points?.length > 0 ? `
                    <section class="notes-section">
                        <h2><span class="icon">-</span> Key Points <span class="section-badge">${meeting.key_points.length}</span></h2>
                        <ul class="key-points-list">
                            ${meeting.key_points.map(point => `
                                <li>${this._escape(point)}</li>
                            `).join('')}
                        </ul>
                    </section>
                ` : ''}
                
                ${meeting.decisions?.length > 0 ? `
                    <section class="notes-section">
                        <h2><span class="icon">!</span> Decisions <span class="section-badge">${meeting.decisions.length}</span></h2>
                        ${meeting.decisions.map(d => `
                            <div class="decision-item">
                                <div class="decision-text">${this._escape(d.decision || d)}</div>
                                ${d.rationale ? `<div class="decision-rationale">Rationale: ${this._escape(d.rationale)}</div>` : ''}
                                ${d.participants_involved?.length > 0 ? `<div class="decision-participants">By: ${d.participants_involved.join(', ')}</div>` : ''}
                            </div>
                        `).join('')}
                    </section>
                ` : ''}
                
                ${meeting.action_items?.length > 0 ? `
                    <section class="notes-section">
                        <h2><span class="icon">#</span> Action Items <span class="section-badge">${meeting.action_items.length}</span></h2>
                        <ul class="action-items-list">
                            ${meeting.action_items.map(item => `
                                <li class="action-item">
                                    <span class="action-task">${this._escape(item.task || item.description || item)}</span>
                                    ${item.assignee ? `<span class="assignee">@ ${this._escape(item.assignee)}</span>` : ''}
                                    ${item.context ? `<div class="action-context">${this._escape(item.context)}</div>` : ''}
                                </li>
                            `).join('')}
                        </ul>
                    </section>
                ` : ''}
                
                ${meeting.open_questions?.length > 0 ? `
                    <section class="notes-section">
                        <h2><span class="icon">?</span> Open Questions <span class="section-badge">${meeting.open_questions.length}</span></h2>
                        <ul class="questions-list">
                            ${meeting.open_questions.map(q => `
                                <li>${this._escape(q)}</li>
                            `).join('')}
                        </ul>
                    </section>
                ` : ''}
                
                ${meeting.transcript?.length > 0 ? `
                    <section class="notes-section">
                        <h2><span class="icon">&gt;</span> Transcript <span class="section-badge">${meeting.transcript.length}</span></h2>
                        <div class="transcript-container">
                            ${meeting.transcript.map(entry => `
                                <div class="transcript-entry">
                                    <span class="speaker">${this._escape(entry.speaker || 'Speaker')}:</span>
                                    <span class="text">${this._escape(entry.text)}</span>
                                </div>
                            `).join('')}
                        </div>
                    </section>
                ` : ''}
            </div>
        `;
        
        // Auto-scroll transcript to bottom
        const transcriptEl = this.container.querySelector('.transcript-container');
        if (transcriptEl) {
            transcriptEl.scrollTop = transcriptEl.scrollHeight;
        }
    }

    _escape(str) {
        if (!str) return '';
        if (typeof str !== 'string') return String(str);
        return str
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    _formatMultiline(str) {
        if (!str) return '';
        return this._escape(str).replace(/\n\n/g, '</p><p>').replace(/\n/g, '<br>');
    }

    showInterimTranscript(text, speaker) {
        let interim = document.getElementById('interim-transcript');
        if (!interim) {
            interim = document.createElement('div');
            interim.id = 'interim-transcript';
            interim.className = 'interim-transcript';
            this.container.prepend(interim);
        }
        interim.innerHTML = `<span class="speaker">${this._escape(speaker) || 'Speaking'}:</span> ${this._escape(text)}`;
    }

    hideInterimTranscript() {
        const interim = document.getElementById('interim-transcript');
        if (interim) interim.remove();
    }
}


if (typeof module !== 'undefined') {
    module.exports = { MeetingClient, MeetingNotesRenderer };
}

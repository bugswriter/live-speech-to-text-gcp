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
            // Get microphone access
            this.audioStream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    channelCount: 1,
                    sampleRate: 16000,  // Match Google Speech API
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true,
                }
            });
            
            // Create AudioContext at 16kHz to match Google Speech API
            this.audioContext = new AudioContext({ sampleRate: 16000 });
            
            // Load the AudioWorklet processor
            await this.audioContext.audioWorklet.addModule('/static/audio-processor.js');
            
            // Create source from microphone
            const source = this.audioContext.createMediaStreamSource(this.audioStream);
            
            // Create AudioWorklet node
            this.audioWorklet = new AudioWorkletNode(this.audioContext, 'audio-processor');
            
            // Handle audio data from worklet
            this.audioWorklet.port.onmessage = (event) => {
                if (this.ws?.readyState === WebSocket.OPEN) {
                    // event.data is ArrayBuffer of Int16 samples
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
        
        this.container.innerHTML = `
            <div class="meeting-notes">
                <header class="meeting-header">
                    <h1 contenteditable="true" class="meeting-title">${this._escape(meeting.title)}</h1>
                    <div class="meeting-meta">
                        <span class="participants">${meeting.participants?.length > 0 ? meeting.participants.join(', ') : 'No participants yet'}</span>
                        <span class="timestamp">${new Date(meeting.created_at).toLocaleString()}</span>
                    </div>
                </header>
                
                ${meeting.summary ? `
                    <section class="summary">
                        <h2>Summary</h2>
                        <div class="summary-content">${this._formatMultiline(meeting.summary)}</div>
                    </section>
                ` : ''}
                
                ${meeting.key_points?.length > 0 ? `
                    <section class="key-points">
                        <h2>Key Points</h2>
                        <ul>
                            ${meeting.key_points.map(point => `
                                <li>${this._escape(point)}</li>
                            `).join('')}
                        </ul>
                    </section>
                ` : ''}
                
                ${meeting.decisions?.length > 0 ? `
                    <section class="decisions">
                        <h2>Decisions Made</h2>
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
                    <section class="action-items">
                        <h2>Action Items</h2>
                        <ul>
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
                    <section class="open-questions">
                        <h2>Open Questions</h2>
                        <ul>
                            ${meeting.open_questions.map(q => `
                                <li>${this._escape(q)}</li>
                            `).join('')}
                        </ul>
                    </section>
                ` : ''}
                
                <section class="transcript">
                    <h2>Transcript <span class="transcript-count">(${meeting.transcript?.length || 0} entries)</span></h2>
                    <div class="transcript-entries">
                        ${(meeting.transcript || []).map(entry => `
                            <div class="transcript-entry">
                                <span class="speaker">${this._escape(entry.speaker)}:</span>
                                <span class="text">${this._escape(entry.text)}</span>
                            </div>
                        `).join('')}
                    </div>
                </section>
            </div>
        `;
        
        // Auto-scroll transcript to bottom
        const transcriptEl = this.container.querySelector('.transcript-entries');
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

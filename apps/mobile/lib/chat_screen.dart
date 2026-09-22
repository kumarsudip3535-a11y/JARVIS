import 'dart:async';
import 'package:flutter/material.dart';
import 'package:speech_to_text/speech_to_text.dart' as stt;
import 'package:flutter_tts/flutter_tts.dart';
import 'api_service.dart';
import 'login_screen.dart';
import 'memory_screen.dart';

class ChatMessageItem {
  final String role; // 'user' or 'assistant'
  final String content;
  ChatMessageItem(this.role, this.content);
}

enum VoiceStatus { idle, listening, thinking, speaking }

class ChatScreen extends StatefulWidget {
  const ChatScreen({super.key});

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> {
  final List<ChatMessageItem> _messages = [];
  final _inputController = TextEditingController();
  final _scrollController = ScrollController();

  bool _checking = true;
  bool _sending = false;
  String? _error;
  String _userEmail = '';
  int? _conversationId;

  // Manual voice-to-text (tap mic, fills the text box) — used outside voice-chat mode.
  final stt.SpeechToText _speech = stt.SpeechToText();
  final FlutterTts _tts = FlutterTts();
  bool _speechAvailable = false;
  bool _listening = false;
  bool _speakReplies = false;

  // Hands-free voice chat mode: tap once, then just talk — JARVIS listens,
  // replies out loud, and starts listening again automatically.
  bool _voiceMode = false;
  VoiceStatus _voiceStatus = VoiceStatus.idle;
  // Whether the mic stays live while JARVIS is speaking, so you can cut it off
  // mid-reply. OFF by default: on a phone speaker, the mic picks up JARVIS's
  // own voice and mistakes it for you talking, so it ends up replying to
  // itself forever. Only turn this on with earphones/headphones, which remove
  // that feedback path.
  bool _allowBargeIn = false;
  // The plugin's own "finalResult" flag can simply never arrive on some
  // devices/Android versions, the same way Chrome's continuous mode can on
  // web. These let us decide for ourselves that you've stopped talking (no
  // new words for ~1.4s) instead of waiting forever for a signal that never
  // comes.
  String _pendingVoiceText = '';
  Timer? _voiceSilenceTimer;

  @override
  void initState() {
    super.initState();
    _checkAuth();
    _initVoice();
  }

  @override
  void dispose() {
    _voiceSilenceTimer?.cancel();
    _speech.stop();
    _tts.stop();
    super.dispose();
  }

  Future<void> _initVoice() async {
    final available = await _speech.initialize(
      onStatus: _onSpeechStatus,
      onError: (_) {
        if (mounted) setState(() => _listening = false);
        if (_voiceMode) {
          Future.delayed(const Duration(milliseconds: 400), () {
            if (!mounted || !_voiceMode) return;
            if (_voiceStatus == VoiceStatus.listening) {
              _startListeningSession();
            } else if (_voiceStatus == VoiceStatus.speaking && _allowBargeIn) {
              _startListeningSession(updateStatus: false);
            }
          });
        }
      },
    );
    _tts.setCompletionHandler(() {
      if (!mounted) return;
      if (_voiceMode) {
        if (_voiceStatus == VoiceStatus.speaking) {
          // If you already interrupted (barge-in already flipped status to
          // "listening"), don't stomp on that.
          setState(() => _voiceStatus = VoiceStatus.listening);
          // With barge-in off, the mic was muted while JARVIS spoke — start
          // it fresh now that it's your turn. With barge-in on, it's already
          // running (started back in _sendVoiceMessage).
          if (!_allowBargeIn) _startListeningSession(updateStatus: false);
        }
      } else {
        setState(() => _voiceStatus = VoiceStatus.idle);
      }
    });
    if (mounted) setState(() => _speechAvailable = available);
  }

  void _onSpeechStatus(String status) {
    // The recognizer stopped on its own (e.g. silence timeout) without ever
    // producing a usable final result. If we're still in voice-chat mode,
    // start a fresh listening session appropriate to whatever we're doing
    // right now (waiting for you, or keeping an ear open while speaking).
    if ((status == 'done' || status == 'notListening') && _voiceMode) {
      if (_voiceStatus == VoiceStatus.listening) {
        Future.delayed(const Duration(milliseconds: 300), () {
          if (mounted && _voiceMode && _voiceStatus == VoiceStatus.listening) {
            _startListeningSession();
          }
        });
      } else if (_voiceStatus == VoiceStatus.speaking && _allowBargeIn) {
        Future.delayed(const Duration(milliseconds: 300), () {
          if (mounted && _voiceMode && _voiceStatus == VoiceStatus.speaking && _allowBargeIn) {
            _startListeningSession(updateStatus: false);
          }
        });
      }
    }
  }

  // ---- Manual tap-to-talk (fills the input box, used outside voice mode) ----

  Future<void> _toggleListening() async {
    if (!_speechAvailable) return;
    if (_listening) {
      await _speech.stop();
      setState(() => _listening = false);
      return;
    }
    setState(() => _listening = true);
    await _speech.listen(
      onResult: (result) {
        setState(() {
          _inputController.text = result.recognizedWords;
          _inputController.selection = TextSelection.fromPosition(
            TextPosition(offset: _inputController.text.length),
          );
        });
      },
    );
  }

  Future<void> _speak(String text) async {
    await _tts.stop();
    await _tts.speak(text);
  }

  // ---- Hands-free voice chat mode ----

  Future<void> _toggleVoiceMode() async {
    if (_voiceMode) {
      setState(() {
        _voiceMode = false;
        _voiceStatus = VoiceStatus.idle;
      });
      _voiceSilenceTimer?.cancel();
      _voiceSilenceTimer = null;
      _pendingVoiceText = '';
      await _speech.stop();
      await _tts.stop();
    } else {
      // Stop any manual listening/speaking first so they don't collide.
      if (_listening) {
        await _speech.stop();
        setState(() => _listening = false);
      }
      await _tts.stop();
      setState(() => _voiceMode = true);
      _startListeningSession();
    }
  }

  /// Starts (or resumes) a speech-recognition session. When [updateStatus] is
  /// true (the normal "it's your turn" case) it also flips the status to
  /// listening; when false, it's a background listen kept alive while JARVIS
  /// is speaking, purely so we can detect if you start talking over it.
  Future<void> _startListeningSession({bool updateStatus = true}) async {
    if (!_speechAvailable || !mounted || !_voiceMode) return;
    if (updateStatus) setState(() => _voiceStatus = VoiceStatus.listening);
    if (_speech.isListening) return;
    await _speech.listen(
      onResult: _onVoiceResult,
      listenFor: const Duration(seconds: 60),
      pauseFor: const Duration(seconds: 3),
      partialResults: true,
    );
  }

  // Typed as dynamic rather than stt.SpeechRecognitionResult: the exact
  // export name has moved between speech_to_text package versions, and this
  // avoids depending on it — result.recognizedWords / .finalResult still
  // resolve fine at runtime regardless of the installed version.
  void _onVoiceResult(dynamic result) {
    if (!mounted || !_voiceMode) return;
    final String text = (result.recognizedWords ?? '').toString().trim();
    final bool isFinal = result.finalResult == true;

    if (text.isEmpty) {
      if (isFinal && _voiceStatus == VoiceStatus.listening) {
        _startListeningSession();
      }
      return;
    }

    if (_allowBargeIn && _voiceStatus == VoiceStatus.speaking) {
      // You started talking while JARVIS was still speaking — cut it off
      // immediately, the same as a person going quiet when interrupted.
      // (Only reachable when barge-in is on; otherwise the mic isn't running
      // during "speaking" at all.)
      _tts.stop();
      setState(() => _voiceStatus = VoiceStatus.listening);
    }

    if (_voiceStatus != VoiceStatus.listening) return;

    _pendingVoiceText = text;
    _voiceSilenceTimer?.cancel();

    if (isFinal) {
      _finalizeVoiceTurn();
    } else {
      // The plugin sometimes never marks a result final even after you've
      // clearly stopped talking. Treat ~1.4s with no new words as "you're
      // done" so the conversation can't get stuck waiting for a signal that
      // may never come.
      _voiceSilenceTimer = Timer(const Duration(milliseconds: 1400), _finalizeVoiceTurn);
    }
  }

  void _finalizeVoiceTurn() {
    _voiceSilenceTimer?.cancel();
    _voiceSilenceTimer = null;
    final text = _pendingVoiceText.trim();
    _pendingVoiceText = '';
    if (text.isNotEmpty && _voiceStatus == VoiceStatus.listening) {
      _sendVoiceMessage(text);
    }
  }

  Future<void> _sendVoiceMessage(String text) async {
    if (_speech.isListening) await _speech.stop();
    setState(() {
      _voiceStatus = VoiceStatus.thinking;
      _messages.add(ChatMessageItem('user', text));
      _error = null;
    });
    _scrollToBottom();

    try {
      final result = await ApiService.sendChatMessage(text, _conversationId);
      final reply = (result['reply'] ?? '').toString();
      setState(() {
        _conversationId = result['conversation_id'] as int?;
        _messages.add(ChatMessageItem('assistant', reply));
      });
      _scrollToBottom();

      if (reply.isNotEmpty) {
        setState(() => _voiceStatus = VoiceStatus.speaking);
        if (_allowBargeIn) {
          // Start listening again right away, without changing status, so
          // that if you talk over JARVIS we hear it and cut it off. Only
          // safe with headphones — otherwise the mic hears JARVIS's own
          // voice through the speaker and JARVIS ends up replying to itself.
          _startListeningSession(updateStatus: false);
        } else if (_speech.isListening) {
          await _speech.stop();
        }
        await _tts.stop();
        await _tts.speak(reply);
        // _tts.setCompletionHandler moves us back to "listening" (and starts
        // the mic again if barge-in is off) once JARVIS finishes speaking.
      } else if (_voiceMode) {
        _startListeningSession();
      } else {
        setState(() => _voiceStatus = VoiceStatus.idle);
      }
    } catch (e) {
      setState(() {
        _error = e.toString().replaceFirst('ApiException: ', '');
      });
      if (_voiceMode) {
        _startListeningSession();
      } else {
        setState(() => _voiceStatus = VoiceStatus.idle);
      }
    }
  }

  String _statusLabel(VoiceStatus s) {
    switch (s) {
      case VoiceStatus.listening:
        return "Listening... just speak";
      case VoiceStatus.thinking:
        return "JARVIS is thinking...";
      case VoiceStatus.speaking:
        return _allowBargeIn ? "JARVIS is speaking... (talk to interrupt)" : "JARVIS is speaking...";
      case VoiceStatus.idle:
        return "Starting...";
    }
  }

  String _statusEmoji(VoiceStatus s) {
    switch (s) {
      case VoiceStatus.listening:
        return "🎤";
      case VoiceStatus.thinking:
        return "💭";
      case VoiceStatus.speaking:
        return "🔊";
      case VoiceStatus.idle:
        return "💭";
    }
  }

  Future<void> _checkAuth() async {
    final token = await ApiService.getToken();
    if (token == null) {
      _goToLogin();
      return;
    }
    try {
      final user = await ApiService.getCurrentUser();
      setState(() {
        _userEmail = user['email'] ?? '';
        _checking = false;
      });
    } catch (_) {
      await ApiService.clearToken();
      _goToLogin();
    }
  }

  void _goToLogin() {
    if (!mounted) return;
    Navigator.of(context).pushReplacement(
      MaterialPageRoute(builder: (_) => const LoginScreen()),
    );
  }

  Future<void> _handleSend() async {
    final text = _inputController.text.trim();
    if (text.isEmpty || _sending) return;

    setState(() {
      _messages.add(ChatMessageItem('user', text));
      _inputController.clear();
      _error = null;
      _sending = true;
    });
    _scrollToBottom();

    try {
      final result = await ApiService.sendChatMessage(text, _conversationId);
      final reply = result['reply'] ?? '';
      setState(() {
        _conversationId = result['conversation_id'] as int?;
        _messages.add(ChatMessageItem('assistant', reply));
      });
      if (_speakReplies && reply.isNotEmpty) {
        _speak(reply);
      }
    } catch (e) {
      setState(() {
        _error = e.toString().replaceFirst('ApiException: ', '');
      });
    } finally {
      setState(() => _sending = false);
      _scrollToBottom();
    }
  }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scrollController.hasClients) {
        _scrollController.animateTo(
          _scrollController.position.maxScrollExtent,
          duration: const Duration(milliseconds: 250),
          curve: Curves.easeOut,
        );
      }
    });
  }

  Future<void> _handleLogout() async {
    if (_voiceMode) await _toggleVoiceMode();
    await ApiService.clearToken();
    _goToLogin();
  }

  void _showAddUserDialog() {
    final fullNameController = TextEditingController();
    final emailController = TextEditingController();
    final passwordController = TextEditingController();
    bool busy = false;
    String? error;
    String? success;

    showDialog(
      context: context,
      builder: (dialogContext) {
        return StatefulBuilder(
          builder: (context, setDialogState) {
            Future<void> submit() async {
              setDialogState(() {
                busy = true;
                error = null;
                success = null;
              });
              try {
                await ApiService.addUser(
                  emailController.text.trim(),
                  passwordController.text,
                  fullNameController.text.trim(),
                );
                setDialogState(() {
                  success = 'Account created for ${emailController.text.trim()}.';
                  emailController.clear();
                  passwordController.clear();
                  fullNameController.clear();
                });
              } catch (e) {
                setDialogState(() {
                  error = e.toString().replaceFirst('ApiException: ', '');
                });
              } finally {
                setDialogState(() => busy = false);
              }
            }

            return AlertDialog(
              title: const Text('Add a new user'),
              content: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  TextField(
                    controller: fullNameController,
                    decoration: const InputDecoration(hintText: 'Full name'),
                  ),
                  TextField(
                    controller: emailController,
                    keyboardType: TextInputType.emailAddress,
                    decoration: const InputDecoration(hintText: 'Email'),
                  ),
                  TextField(
                    controller: passwordController,
                    obscureText: true,
                    decoration: const InputDecoration(hintText: 'Password'),
                  ),
                  if (error != null) ...[
                    const SizedBox(height: 8),
                    Text(error!, style: const TextStyle(color: Colors.red)),
                  ],
                  if (success != null) ...[
                    const SizedBox(height: 8),
                    Text(success!, style: const TextStyle(color: Colors.green)),
                  ],
                ],
              ),
              actions: [
                TextButton(
                  onPressed: () => Navigator.of(dialogContext).pop(),
                  child: const Text('Close'),
                ),
                ElevatedButton(
                  onPressed: busy ? null : submit,
                  child: Text(busy ? 'Creating...' : 'Create account'),
                ),
              ],
            );
          },
        );
      },
    );
  }

  @override
  Widget build(BuildContext context) {
    if (_checking) {
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }

    return Scaffold(
      appBar: AppBar(
        title: const Text('JARVIS'),
        actions: [
          if (_speechAvailable)
            IconButton(
              onPressed: _toggleVoiceMode,
              icon: Icon(_voiceMode ? Icons.stop_circle : Icons.graphic_eq),
              color: _voiceMode ? Colors.red : null,
              tooltip: _voiceMode ? 'End voice chat' : 'Start voice chat',
            ),
          if (_speechAvailable)
            IconButton(
              onPressed: () => setState(() => _allowBargeIn = !_allowBargeIn),
              icon: Icon(_allowBargeIn ? Icons.headset : Icons.headset_off),
              tooltip: _allowBargeIn
                  ? 'Interrupting JARVIS: on (needs headphones)'
                  : 'Interrupting JARVIS: off — turn on only with headphones',
            ),
          IconButton(
            onPressed: () => setState(() => _speakReplies = !_speakReplies),
            icon: Icon(_speakReplies ? Icons.volume_up : Icons.volume_off),
            tooltip: _speakReplies ? 'Voice replies: on' : 'Voice replies: off',
          ),
          IconButton(
            onPressed: () => Navigator.of(context).push(
              MaterialPageRoute(builder: (_) => const MemoryScreen()),
            ),
            icon: const Icon(Icons.psychology_alt),
            tooltip: 'Memory',
          ),
          IconButton(
            onPressed: _showAddUserDialog,
            icon: const Icon(Icons.person_add),
            tooltip: 'Add user',
          ),
          IconButton(
            onPressed: _handleLogout,
            icon: const Icon(Icons.logout),
            tooltip: 'Sign out',
          ),
        ],
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(20),
          child: Padding(
            padding: const EdgeInsets.only(bottom: 6),
            child: Text(
              'Signed in as $_userEmail',
              style: TextStyle(fontSize: 12, color: Colors.grey[600]),
            ),
          ),
        ),
      ),
      body: SafeArea(
        child: Column(
          children: [
            Expanded(
              child: _messages.isEmpty
                  ? Center(
                      child: Text(
                        'Say hello to JARVIS to start a conversation.',
                        style: TextStyle(color: Colors.grey[400]),
                      ),
                    )
                  : ListView.builder(
                      controller: _scrollController,
                      padding: const EdgeInsets.all(12),
                      itemCount: _messages.length + (_sending ? 1 : 0),
                      itemBuilder: (context, index) {
                        if (index == _messages.length) {
                          return _bubble('JARVIS is thinking...', false);
                        }
                        final m = _messages[index];
                        return _bubble(m.content, m.role == 'user');
                      },
                    ),
            ),
            if (_error != null)
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 12),
                child: Text(_error!, style: const TextStyle(color: Colors.red)),
              ),
            SafeArea(
              top: false,
              child: _voiceMode
                  ? _buildVoiceStatusBar()
                  : Padding(
                      padding: const EdgeInsets.all(12),
                      child: Row(
                        children: [
                          if (_speechAvailable)
                            IconButton(
                              onPressed: _toggleListening,
                              icon: Icon(_listening ? Icons.mic : Icons.mic_none),
                              color: _listening ? Colors.red : null,
                              tooltip: _listening ? 'Stop listening' : 'Speak your message',
                            ),
                          Expanded(
                            child: TextField(
                              controller: _inputController,
                              decoration: InputDecoration(
                                hintText: _listening ? 'Listening...' : 'Message JARVIS...',
                                border: const OutlineInputBorder(),
                                contentPadding: const EdgeInsets.symmetric(
                                  horizontal: 12,
                                  vertical: 10,
                                ),
                              ),
                              onSubmitted: (_) => _handleSend(),
                            ),
                          ),
                          const SizedBox(width: 8),
                          ElevatedButton(
                            onPressed: _sending ? null : _handleSend,
                            style: ElevatedButton.styleFrom(
                              backgroundColor: Colors.black,
                              foregroundColor: Colors.white,
                            ),
                            child: const Text('Send'),
                          ),
                        ],
                      ),
                    ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildVoiceStatusBar() {
    final isListening = _voiceStatus == VoiceStatus.listening;
    final isSpeaking = _voiceStatus == VoiceStatus.speaking;
    final circleColor = isListening
        ? Colors.red[100]
        : isSpeaking
            ? Colors.blue[100]
            : Colors.grey[200];

    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 16),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 64,
            height: 64,
            decoration: BoxDecoration(
              color: circleColor,
              shape: BoxShape.circle,
            ),
            child: Center(
              child: Text(
                _statusEmoji(_voiceStatus),
                style: const TextStyle(fontSize: 28),
              ),
            ),
          ),
          const SizedBox(height: 10),
          Text(
            _statusLabel(_voiceStatus),
            style: const TextStyle(fontSize: 14, color: Colors.black87),
          ),
          const SizedBox(height: 2),
          Text(
            _allowBargeIn
                ? 'Just speak — even to cut JARVIS off mid-reply'
                : "Just speak — JARVIS will listen again once it's done talking",
            style: TextStyle(fontSize: 12, color: Colors.grey[500]),
            textAlign: TextAlign.center,
          ),
        ],
      ),
    );
  }

  Widget _bubble(String text, bool isUser) {
    return Align(
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        margin: const EdgeInsets.symmetric(vertical: 4),
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        constraints: BoxConstraints(
          maxWidth: MediaQuery.of(context).size.width * 0.8,
        ),
        decoration: BoxDecoration(
          color: isUser ? Colors.black : Colors.grey[200],
          borderRadius: BorderRadius.circular(16),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Flexible(
              child: Text(
                text,
                style: TextStyle(color: isUser ? Colors.white : Colors.black87),
              ),
            ),
            if (!isUser) ...[
              const SizedBox(width: 6),
              GestureDetector(
                onTap: () => _speak(text),
                child: const Icon(Icons.volume_up, size: 16, color: Colors.grey),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

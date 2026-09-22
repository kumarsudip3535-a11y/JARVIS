import 'dart:convert';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

/// Change this to your PC's LAN IPv4 address (from `ipconfig`).
/// Your phone and PC must be on the same Wi-Fi network.
const String kApiBaseUrl = 'http://192.168.1.9:8000';

class ApiException implements Exception {
  final String message;
  ApiException(this.message);
  @override
  String toString() => message;
}

class ApiService {
  static const _tokenKey = 'jarvis_token';

  static Future<String?> getToken() async {
    final prefs = await SharedPreferences.getInstance();
    return prefs.getString(_tokenKey);
  }

  static Future<void> setToken(String token) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_tokenKey, token);
  }

  static Future<void> clearToken() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_tokenKey);
  }

  static Future<Map<String, String>> _authHeaders() async {
    final token = await getToken();
    return {
      'Content-Type': 'application/json',
      if (token != null) 'Authorization': 'Bearer $token',
    };
  }

  static Future<dynamic> _handleResponse(http.Response res) async {
    if (res.statusCode >= 200 && res.statusCode < 300) {
      if (res.body.isEmpty) return null;
      return jsonDecode(res.body);
    }
    String message = 'Request failed (${res.statusCode})';
    try {
      final body = jsonDecode(res.body);
      if (body is Map && body['detail'] != null) {
        message = body['detail'].toString();
      }
    } catch (_) {}
    throw ApiException(message);
  }

  static Future<void> registerUser(
    String email,
    String password,
    String fullName,
  ) async {
    // Only works when JARVIS has zero accounts yet (fresh install / bootstrap).
    // Once an owner account exists, the backend requires addUser() (authenticated) instead.
    final res = await http.post(
      Uri.parse('$kApiBaseUrl/api/auth/register'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({
        'email': email,
        'password': password,
        'full_name': fullName,
      }),
    );
    await _handleResponse(res);
  }

  static Future<void> addUser(
    String email,
    String password,
    String fullName,
  ) async {
    // Create an additional account while logged in — e.g. adding a family member.
    final res = await http.post(
      Uri.parse('$kApiBaseUrl/api/auth/register'),
      headers: await _authHeaders(),
      body: jsonEncode({
        'email': email,
        'password': password,
        'full_name': fullName,
      }),
    );
    await _handleResponse(res);
  }

  static Future<String> loginUser(String email, String password) async {
    final res = await http.post(
      Uri.parse('$kApiBaseUrl/api/auth/login'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'email': email, 'password': password}),
    );
    final data = await _handleResponse(res);
    final token = data['access_token'] as String;
    await setToken(token);
    return token;
  }

  static Future<Map<String, dynamic>> getCurrentUser() async {
    final res = await http.get(
      Uri.parse('$kApiBaseUrl/api/auth/me'),
      headers: await _authHeaders(),
    );
    return await _handleResponse(res) as Map<String, dynamic>;
  }

  static Future<Map<String, dynamic>> sendChatMessage(
    String message,
    int? conversationId,
  ) async {
    final res = await http.post(
      Uri.parse('$kApiBaseUrl/api/chat/message'),
      headers: await _authHeaders(),
      body: jsonEncode({
        'message': message,
        if (conversationId != null) 'conversation_id': conversationId,
      }),
    );
    return await _handleResponse(res) as Map<String, dynamic>;
  }

  // Phase 9 (memory): view/edit/delete what JARVIS remembers about you.
  // Saving happens automatically after chat replies (on the backend) — these
  // calls are for the Memory screen, not the chat flow itself.
  static Future<List<dynamic>> listMemories() async {
    final res = await http.get(
      Uri.parse('$kApiBaseUrl/api/memory'),
      headers: await _authHeaders(),
    );
    return await _handleResponse(res) as List<dynamic>;
  }

  static Future<Map<String, dynamic>> createMemory(
    String content,
    String? category,
  ) async {
    final res = await http.post(
      Uri.parse('$kApiBaseUrl/api/memory'),
      headers: await _authHeaders(),
      body: jsonEncode({'content': content, 'category': category}),
    );
    return await _handleResponse(res) as Map<String, dynamic>;
  }

  static Future<Map<String, dynamic>> updateMemory(
    int id,
    String content,
    String? category,
  ) async {
    final res = await http.put(
      Uri.parse('$kApiBaseUrl/api/memory/$id'),
      headers: await _authHeaders(),
      body: jsonEncode({'content': content, 'category': category}),
    );
    return await _handleResponse(res) as Map<String, dynamic>;
  }

  static Future<void> deleteMemory(int id) async {
    final res = await http.delete(
      Uri.parse('$kApiBaseUrl/api/memory/$id'),
      headers: await _authHeaders(),
    );
    await _handleResponse(res);
  }
}

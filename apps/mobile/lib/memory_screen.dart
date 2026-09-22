import 'package:flutter/material.dart';
import 'api_service.dart';

const List<String> kMemoryCategories = [
  'general',
  'business',
  'family',
  'preference',
  'project',
];

class MemoryItem {
  final int id;
  final String content;
  final String? category;
  final String source; // "auto" or "manual"

  MemoryItem({
    required this.id,
    required this.content,
    required this.category,
    required this.source,
  });

  factory MemoryItem.fromJson(Map<String, dynamic> json) {
    return MemoryItem(
      id: json['id'] as int,
      content: (json['content'] ?? '').toString(),
      category: json['category'] as String?,
      source: (json['source'] ?? 'auto').toString(),
    );
  }
}

class MemoryScreen extends StatefulWidget {
  const MemoryScreen({super.key});

  @override
  State<MemoryScreen> createState() => _MemoryScreenState();
}

class _MemoryScreenState extends State<MemoryScreen> {
  List<MemoryItem> _memories = [];
  bool _loading = true;
  String? _error;

  final _newContentController = TextEditingController();
  String _newCategory = 'general';
  bool _adding = false;

  @override
  void initState() {
    super.initState();
    _refresh();
  }

  @override
  void dispose() {
    _newContentController.dispose();
    super.dispose();
  }

  Future<void> _refresh() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final data = await ApiService.listMemories();
      final items = data
          .map((e) => MemoryItem.fromJson(e as Map<String, dynamic>))
          .toList();
      if (!mounted) return;
      setState(() => _memories = items);
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = e.toString().replaceFirst('ApiException: ', ''));
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _handleAdd() async {
    final content = _newContentController.text.trim();
    if (content.isEmpty || _adding) return;
    setState(() => _adding = true);
    try {
      await ApiService.createMemory(content, _newCategory);
      _newContentController.clear();
      await _refresh();
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = e.toString().replaceFirst('ApiException: ', ''));
    } finally {
      if (mounted) setState(() => _adding = false);
    }
  }

  Future<void> _handleDelete(MemoryItem m) async {
    try {
      await ApiService.deleteMemory(m.id);
      if (!mounted) return;
      setState(() => _memories.removeWhere((x) => x.id == m.id));
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = e.toString().replaceFirst('ApiException: ', ''));
    }
  }

  void _showEditDialog(MemoryItem m) {
    final contentController = TextEditingController(text: m.content);
    String category = m.category ?? 'general';
    bool busy = false;
    String? dialogError;

    showDialog(
      context: context,
      builder: (dialogContext) {
        return StatefulBuilder(
          builder: (context, setDialogState) {
            Future<void> submit() async {
              final newContent = contentController.text.trim();
              if (newContent.isEmpty) return;
              setDialogState(() {
                busy = true;
                dialogError = null;
              });
              try {
                await ApiService.updateMemory(m.id, newContent, category);
                if (dialogContext.mounted) Navigator.of(dialogContext).pop();
                await _refresh();
              } catch (e) {
                setDialogState(() {
                  dialogError = e.toString().replaceFirst('ApiException: ', '');
                });
              } finally {
                setDialogState(() => busy = false);
              }
            }

            return AlertDialog(
              title: const Text('Edit memory'),
              content: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  TextField(
                    controller: contentController,
                    maxLines: 3,
                    decoration: const InputDecoration(hintText: 'Memory content'),
                  ),
                  const SizedBox(height: 8),
                  DropdownButton<String>(
                    value: category,
                    isExpanded: true,
                    items: kMemoryCategories
                        .map((c) => DropdownMenuItem(value: c, child: Text(c)))
                        .toList(),
                    onChanged: (v) => setDialogState(() => category = v ?? category),
                  ),
                  if (dialogError != null) ...[
                    const SizedBox(height: 8),
                    Text(dialogError!, style: const TextStyle(color: Colors.red)),
                  ],
                ],
              ),
              actions: [
                TextButton(
                  onPressed: () => Navigator.of(dialogContext).pop(),
                  child: const Text('Cancel'),
                ),
                ElevatedButton(
                  onPressed: busy ? null : submit,
                  style: ElevatedButton.styleFrom(
                    backgroundColor: Colors.black,
                    foregroundColor: Colors.white,
                  ),
                  child: Text(busy ? 'Saving...' : 'Save'),
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
    return Scaffold(
      appBar: AppBar(
        title: const Text('🧠 Memory'),
      ),
      body: SafeArea(
        child: Column(
          children: [
            Padding(
              padding: const EdgeInsets.all(12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text(
                    'Add a memory manually',
                    style: TextStyle(fontWeight: FontWeight.w600),
                  ),
                  const SizedBox(height: 6),
                  TextField(
                    controller: _newContentController,
                    maxLines: 2,
                    decoration: const InputDecoration(
                      hintText: 'e.g. I prefer short, direct answers',
                      border: OutlineInputBorder(),
                    ),
                  ),
                  const SizedBox(height: 6),
                  Row(
                    children: [
                      DropdownButton<String>(
                        value: _newCategory,
                        items: kMemoryCategories
                            .map((c) => DropdownMenuItem(value: c, child: Text(c)))
                            .toList(),
                        onChanged: (v) =>
                            setState(() => _newCategory = v ?? _newCategory),
                      ),
                      const Spacer(),
                      ElevatedButton(
                        onPressed: _adding ? null : _handleAdd,
                        style: ElevatedButton.styleFrom(
                          backgroundColor: Colors.black,
                          foregroundColor: Colors.white,
                        ),
                        child: Text(_adding ? 'Saving...' : 'Save memory'),
                      ),
                    ],
                  ),
                ],
              ),
            ),
            if (_error != null)
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
                child: Text(_error!, style: const TextStyle(color: Colors.red)),
              ),
            const Divider(height: 1),
            Expanded(
              child: _loading
                  ? const Center(child: CircularProgressIndicator())
                  : _memories.isEmpty
                      ? Center(
                          child: Padding(
                            padding: const EdgeInsets.all(24),
                            child: Text(
                              'Nothing remembered yet — JARVIS will save '
                              'durable facts here as you chat, or add one '
                              'yourself above.',
                              textAlign: TextAlign.center,
                              style: TextStyle(color: Colors.grey[400]),
                            ),
                          ),
                        )
                      : RefreshIndicator(
                          onRefresh: _refresh,
                          child: ListView.builder(
                            padding: const EdgeInsets.all(12),
                            itemCount: _memories.length,
                            itemBuilder: (context, index) {
                              final m = _memories[index];
                              return Card(
                                margin: const EdgeInsets.only(bottom: 8),
                                child: ListTile(
                                  title: Text(m.content),
                                  subtitle: Text(
                                    '${m.category ?? 'general'} · '
                                    '${m.source == 'auto' ? 'learned automatically' : 'added manually'}',
                                    style: const TextStyle(fontSize: 12),
                                  ),
                                  trailing: Row(
                                    mainAxisSize: MainAxisSize.min,
                                    children: [
                                      IconButton(
                                        icon: const Icon(Icons.edit, size: 20),
                                        tooltip: 'Edit',
                                        onPressed: () => _showEditDialog(m),
                                      ),
                                      IconButton(
                                        icon: const Icon(Icons.delete,
                                            size: 20, color: Colors.red),
                                        tooltip: 'Delete',
                                        onPressed: () => _handleDelete(m),
                                      ),
                                    ],
                                  ),
                                ),
                              );
                            },
                          ),
                        ),
            ),
          ],
        ),
      ),
    );
  }
}

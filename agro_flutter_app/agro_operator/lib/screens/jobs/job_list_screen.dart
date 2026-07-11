// lib/screens/jobs/job_list_screen.dart
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../../providers/job_provider.dart';
import '../../providers/language_provider.dart';
import '../../widgets/job_card.dart';
import 'job_detail_screen.dart';

class JobListScreen extends StatefulWidget {
  const JobListScreen({super.key});
  @override
  State<JobListScreen> createState() => _JobListScreenState();
}

class _JobListScreenState extends State<JobListScreen> {
  String _filterStatus = '';
  String _filterType   = '';

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      context.read<JobProvider>().fetchAllJobs();
    });
  }

  @override
  Widget build(BuildContext context) {
    final lang = context.watch<LanguageProvider>();
    final prov = context.watch<JobProvider>();
    final jobs = prov.jobs;
    final isNe = lang.isNepali;

    var filtered = jobs;
    if (_filterStatus.isNotEmpty) {
      filtered = filtered.where((j) => j.status == _filterStatus).toList();
    }
    if (_filterType.isNotEmpty) {
      filtered = filtered.where((j) => j.jobType == _filterType).toList();
    }

    // Count badges
    int countOf(String s) => jobs.where((j) => j.status == s).length;

    return Scaffold(
      appBar: AppBar(
        backgroundColor: const Color(0xFF003893),
        foregroundColor: Colors.white,
        title: Text(isNe ? 'सबै कामहरू' : 'All Jobs'),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            tooltip: isNe ? 'ताजा गर्नुस्' : 'Refresh',
            onPressed: () => context.read<JobProvider>().fetchAllJobs(),
          ),
        ],
      ),
      body: Column(
        children: [
          // Summary bar
          Container(
            color: const Color(0xFF003893).withOpacity(0.06),
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
            child: Row(
              mainAxisAlignment: MainAxisAlignment.spaceAround,
              children: [
                _SummaryDot(isNe ? 'पेन्डिंग' : 'Pending',   countOf('pending'),     Colors.orange),
                _SummaryDot(isNe ? 'चलिरहेको' : 'Active',    countOf('in_progress'), Colors.purple),
                _SummaryDot(isNe ? 'सकियो' : 'Done',         countOf('completed'),   Colors.green),
                _SummaryDot(isNe ? 'रद्द' : 'Cancelled',     countOf('cancelled'),   Colors.red),
              ],
            ),
          ),
          // Filter chips
          SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
            child: Row(children: [
              _Chip(isNe ? 'सबै' : 'All', _filterStatus.isEmpty && _filterType.isEmpty,
                  () => setState(() { _filterStatus = ''; _filterType = ''; })),
              const SizedBox(width: 8),
              _Chip(isNe ? 'पेन्डिंग' : 'Pending', _filterStatus == 'pending',
                  () => setState(() { _filterStatus = 'pending'; _filterType = ''; })),
              const SizedBox(width: 8),
              _Chip(isNe ? 'पुष्टि भएको' : 'Confirmed', _filterStatus == 'confirmed',
                  () => setState(() { _filterStatus = 'confirmed'; _filterType = ''; })),
              const SizedBox(width: 8),
              _Chip(isNe ? 'चलिरहेको' : 'In Progress', _filterStatus == 'in_progress',
                  () => setState(() { _filterStatus = 'in_progress'; _filterType = ''; })),
              const SizedBox(width: 8),
              _Chip(isNe ? 'सकियो' : 'Completed', _filterStatus == 'completed',
                  () => setState(() { _filterStatus = 'completed'; _filterType = ''; })),
              const SizedBox(width: 8),
              _Chip(isNe ? 'रद्द' : 'Cancelled', _filterStatus == 'cancelled',
                  () => setState(() { _filterStatus = 'cancelled'; _filterType = ''; }),
                  chipColor: Colors.red.shade100, selectedColor: Colors.red.shade200),
              const SizedBox(width: 8),
              _Chip(isNe ? 'कृषि' : 'Agriculture', _filterType == 'agriculture',
                  () => setState(() { _filterType = 'agriculture'; _filterStatus = ''; })),
              const SizedBox(width: 8),
              _Chip(isNe ? 'यातायात' : 'Transport', _filterType == 'transport',
                  () => setState(() { _filterType = 'transport'; _filterStatus = ''; })),
            ]),
          ),
          Expanded(
            child: prov.loading
                ? const Center(child: CircularProgressIndicator())
                : filtered.isEmpty
                    ? Center(child: Column(
                        mainAxisAlignment: MainAxisAlignment.center,
                        children: [
                          const Icon(Icons.inbox_outlined, size: 52, color: Colors.black12),
                          const SizedBox(height: 8),
                          Text(isNe ? 'कुनै काम फेला परेन' : 'No jobs found',
                              style: const TextStyle(color: Colors.black38)),
                        ],
                      ))
                    : RefreshIndicator(
                        onRefresh: () async => context.read<JobProvider>().fetchAllJobs(),
                        child: ListView.builder(
                          itemCount: filtered.length,
                          itemBuilder: (_, i) => JobCard(
                            job: filtered[i],
                            onTap: () => Navigator.push(
                              context,
                              MaterialPageRoute(
                                builder: (_) => JobDetailScreen(job: filtered[i]),
                              ),
                            ).then((_) => context.read<JobProvider>().fetchAllJobs()),
                          ),
                        ),
                      ),
          ),
        ],
      ),
    );
  }
}

class _SummaryDot extends StatelessWidget {
  final String label;
  final int count;
  final Color color;
  const _SummaryDot(this.label, this.count, this.color);
  @override
  Widget build(BuildContext context) => Row(
    mainAxisSize: MainAxisSize.min,
    children: [
      Container(width: 8, height: 8,
          decoration: BoxDecoration(color: color, shape: BoxShape.circle)),
      const SizedBox(width: 4),
      Text('$count $label', style: TextStyle(fontSize: 11, color: color, fontWeight: FontWeight.w600)),
    ],
  );
}

class _Chip extends StatelessWidget {
  final String label;
  final bool selected;
  final VoidCallback onTap;
  final Color? chipColor;
  final Color? selectedColor;
  const _Chip(this.label, this.selected, this.onTap, {this.chipColor, this.selectedColor});
  @override
  Widget build(BuildContext context) => FilterChip(
    label: Text(label),
    selected: selected,
    onSelected: (_) => onTap(),
    backgroundColor: chipColor,
    selectedColor: selectedColor ?? const Color(0xFF003893).withOpacity(0.2),
    checkmarkColor: selectedColor != null ? Colors.red.shade700 : const Color(0xFF003893),
  );
}

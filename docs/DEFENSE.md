# Müdafiə hazırlığı (Defense notes)

Slayd 25: *"Unexpected jobs submit edilir → scheduler qərar verir → qərar izah olunur."*
Qiymətləndirmədə müdafiə 5 %-dir, amma "niyə?" sualına cavab verə bilmək bütün layihənin qiymətinə təsir edir.
Bu sənəd: (1) müdafiədən əvvəl yoxlama, (2) demo planı, (3) gözlənilməz job-lara reaksiya, (4) sual-cavab.

## 1. Müdafiədən əvvəl (10 dəqiqə)

Tapşırıq slaydı mühiti **Ubuntu 26.04 VM (VirtualBox)** kimi göstərir. Layihə WSL2-də hazırlanıb (eyni Linux kernel
interfeysləri). Müəllimdən əvvəlcədən soruş: *"WSL2 qəbul olunur?"* Əgər VM tələb olunsa:

```bash
git clone <repo-url> && cd mini-hpc-scheduler
bash scripts/setup_wsl2.sh        # mühit yoxlaması (VM-də də işləyir)
make test-fast                    # ~1 san
make test                         # ~1 dəq, real prosesləri işə salır
DEMO_SCALE=0.2 bash scripts/demo.sh defense
```

Yoxla: `python3 --version` (≥ 3.9), `nproc` (4 CPU-dan azdırsa fərq yoxdur, scheduler virtual 4 CPU idarə edir),
repo `~/` altındadır (`/mnt/c` yox).

## 2. Demo planı (5-7 dəqiqə)

1. **Interfeys** (slayd 4): `python3 minisched.py` → `status`, `help`.
2. **Defense testi** (slayd 25): `bash scripts/demo.sh defense` (və ya əl ilə, aşağıda). Göstər:
   `queue` (policy sırası + ETA), `explain` (niyə belə qərar), `stats` (orta gözləmə).
3. **Policy dəyişimi canlı**: `set policy sjf` → eyni job-ları yenidən göndər → nəticə fərqli.
4. **Backfill** (slayd 13): `bash scripts/demo.sh backfill` → `why 3` ("BACKFILL ... without delaying #2").
5. **Process control** (slayd 15-17): `bash scripts/demo.sh control` → `grep State /proc/<pid>/status` (T), `Cpus_allowed_list`, `nice`.
6. **Resource abuse** (slayd 19): `bash scripts/demo.sh abuse` → WARNING → VIOLATION → terminate.
7. **Crash recovery** (slayd 21, bonus): `bash scripts/demo.sh recovery` → `kill -9` scheduler, job-lar sağ qalır, restartdan sonra re-attach.
8. **Innovation**: estimator + ETA (`docs/EXPERIMENTS.md` qrafikləri).

Əl ilə defense testi (A=4 CPU/60 s, B,C,D=1 CPU/5 s):

```bash
python3 minisched.py start --policy fcfs        # sonra: set policy sjf
python3 minisched.py hold                       # bütün job-lar "eyni anda" gəlsin
python3 minisched.py submit --cpu 4 --est 60 python3 workloads/sleep_job.py --seconds 60
for i in 1 2 3; do python3 minisched.py submit --cpu 1 --est 5 python3 workloads/sleep_job.py --seconds 5; done
python3 minisched.py queue; python3 minisched.py release; python3 minisched.py explain
```

**Əl hesabı (yadda saxla):**

| | A başlama | B,C,D başlama | orta gözləmə | orta turnaround | makespan |
|---|---|---|---|---|---|
| FCFS | 0 | 60 | (0+60+60+60)/4 = **45 s** | (60+65+65+65)/4 = **63.75 s** | 65 s |
| SJF | 5 | 0 (3 CPU-da paralel) | (0+0+0+5)/4 = **1.25 s** | (5+5+5+65)/4 = **20 s** | 65 s |

Makespan eynidir (eyni iş, eyni CPU-lar) - dəyişən yalnız **sıradır**. SJF-də A-nın gözləməsi 5 s-dir, amma qısa job-lar axını
gəlsəydi A-nı sonsuz gecikdirə bilərdi (starvation) → aging.

## 3. Gözlənilməz job-lar (müəllim nə göndərə bilər)

| Situasiya | Nə baş verir | Nə göstər |
|---|---|---|
| Mövcud CPU-dan çox istəyir (`--cpu 5`) | `error: cpu must be 1..4` - qəbul olunmur | pool ölçüsü virtualdır |
| Mövcud RAM-dan çox (`--mem 8G`) | rədd edilir | |
| Var olmayan proqram | `FAILED`, exit 127 | `history`, `log ID` |
| Job özü crash edir / exit ≠ 0 | `FAILED`, exit kodu `reason`-da; `--retries N` ilə yenidən cəhd | |
| Job uşaq proses açır və qaçır | `cancel` bütün **process group**-a gedir; valideyn bitəndə qalan uşaqlar SIGKILL olunur | `ps` ilə yoxla |
| Job SIGTERM-i ignor edir | 3 san sonra SIGKILL | `events` |
| Declared 100 MB, real 600 MB | WARNING (1.25×) → VIOLATION (2×, 3 ardıcıl ölçmə) → seçilmiş policy | `events`, `job ID` |
| Sonsuz işləyən job | `--timeout SEC` ilə `TIMEOUT`; yoxsa `cancel` | |
| Çoxlu job eyni anda | batch pəncərəsi (0.5 s): hamısı birlikdə sıralanır | `queue` |
| Scheduler ölür (`kill -9`) | job-lar yaşayır, restartda re-attach | `demo.sh recovery` |
| Müəllim yavaş yazır: A göndərir, 2 san sonra B,C,D | A **artıq başlayıb** - non-preemptive scheduler gələcəyi bilmir | bax: sual 4 |

Canlı dəyişdirmək üçün: `set policy fcfs|sjf|priority`, `set backfill on|off`, `set aging_interval 10`,
`set violation_action pause`, `pause/resume/cancel ID`, `hold/release`.

## 4. Sual-cavab

**1. Arxitekturanı niyə belə qurdun?**
Daemon + stateless CLI + SQLite. Daemon tək thread-dir (`select` loop), buna görə lock yoxdur və "qərar ver → launch et" arasında
race yoxdur. Qərar məntiqi `planner.plan()` təmiz funksiyadır: eyni kod həm real qərarları, həm ETA simulyasiyasını, həm testləri idarə edir.

**2. "Dynamic scheduler loop" burada nədir? Hard-coded sequence deyil?**
Loop `select()`-də üç hadisəni gözləyir: client əmri, SIGCHLD (job bitdi), timer. Hər oyanışda eyni *reconcile* addımı işləyir
(exit-ləri yığ → `/proc` ölç → limitləri yoxla → qərar ver → launch). Nə baş verəcəyi müşahidə olunan vəziyyətdən asılıdır: job bitibsə
resurs millisaniyələr içində azad olur; yaddaş artıbsa enforcement işə düşür; policy dəyişibsə növbəti qərar yeni policy ilə verilir.

**3. Niyə bu testdə SJF FCFS-dən yaxşıdır?**
Əl hesabı (yuxarıdakı cədvəl): orta gözləmə 45 s → 1.25 s. SJF qısa job-ları (B,C,D) əvvəl işlədir, onlar 3 CPU-da paralel bitir; A yalnız
5 s gözləyir. FCFS-də A birinci olduğu üçün hər üç qısa job 60 s gözləyir (convoy effect). Makespan eynidir (65 s).

**4. Əgər A artıq başlayıbsa və sonra B,C,D gəlirsə?**
Scheduler non-preemptive-dir və gələcəyi bilmir: A başlayıbsa, B,C,D gözləməlidir. İki yumşaldıcı: (a) submit batching (0.5 s) və `hold/release`
ilə yaxın gələn job-lar birlikdə sıralanır; (b) `pause` (SIGSTOP) ilə A-nı dayandırmaq olar, amma bu *manual*-dır; avtomatik preemption
Round Robin / preemptive SJF olardı - implementasiya olunmayıb.

**5. SJF-in riski nədir, necə həll etdin?**
Starvation: qısa job-lar axını uzun job-u əbədi gecikdirə bilər. Həll: **aging** - `effective_runtime = est / (1 + waited / (2·aging))`;
gözlədikcə job-un "görünən uzunluğu" azalır (2·aging san-dən sonra yarıya). Priority policy-də `effective = base + waited/aging`.
Eksperiment: aging olmadan LOW job 8.5 s, aging ilə 4.4 s gözləyir (HIGH-ların gözləməsi 1.05 → 2.07 s). Trade-off var.

**6. SJF üçün runtime əvvəlcədən necə bilinir?**
İstifadəçi təxmini (`--est`) + **öyrənən estimator** (innovation): hər əmr sətri + CPU üçün keçmiş real runtime-lar saxlanır;
≥ 2 run varsa EWMA, 1 run varsa (user+run)/2, yoxsa user təxmini, yoxsa 30 s default. 20 s təxmin / 1 s real: 3-cü run-dan düzgün.

**7. Backfill nədir? Head job-u gecikdirmir?**
Növbədə birinci job sığmırsa ona *rezervasiya* verilir: "shadow time" = işləyən job-lar bitdikcə lazımi CPU **və** RAM-ın boşalacağı ən erkən an.
Arxadakı job yalnız (a) indi sığırsa **və** təxmini bitməsi shadow time-dan əvvəldirsə, ya da (b) head-in o anda ehtiyac duymadığı "extra"
resursdan istifadə edirsə başlaya bilər. Təxminlər düzdürsə head heç vaxt gecikmir. Təxmin çox optimist olarsa gecikə bilər - bunun üçün estimator var.
Qeyd: bu halı ayrıca ölçməmişəm (`EXPERIMENTS.md`, "Not tested").

**8. CPU affinity ilə nice fərqi? cgroup ilə?**
Affinity = **harada** (CPU maskası, `sched_setaffinity`; kernel yalnız bu CPU-ları seçir). nice = **nə qədər üstünlük** (CFS çəkisi: nice 0 → 1024,
5 → 335, 15 → 36; yalnız CPU üçün rəqabət olanda təsir edir, rezervasiya deyil). cgroup = **maksimum nə qədər** (`memory.max`, `cpu.max` - sərt limit).
Mən affinity + nice istifadə edirəm; cgroup v2 (bonus) implementasiya **olunmayıb**.

**9. PID niyə `sh` prosesinin PID-idir?**
`sh` wrapper-dir: real əmri işlədir, bitəndə exit kodunu `jobs/<id>.exit` faylına yazır. Scheduler crash edib yenidən başlayanda artıq job-un
valideyni deyil və `wait()` edə bilmir - exit kodunu fayldan oxuyur. PID eyni zamanda **process group leader**-dir (`setsid`), siqnallar `killpg` ilə bütün
ağaca gedir; `/proc` ölçməsi də bütün ağac üzərindədir.

**10. SIGSTOP/SIGCONT/SIGTERM/SIGKILL fərqləri?**
SIGSTOP tutula/ignor edilə bilməz, proses `T` vəziyyətinə keçir (CPU yemir, yaddaş qalır). SIGCONT davam etdirir. SIGTERM nəzakətli
bitirmədir (proses tuta bilər). SIGKILL tutulmur. Cancel: SIGTERM **+ SIGCONT** (dayanmış proses SIGTERM-i işləyə bilməz), 3 san sonra SIGKILL.

**11. Paused job niyə CPU/RAM-ı saxlayır?**
Yaddaşı fiziki olaraq RAM-da qalır; resursu başqasına versəydim, `resume` zamanı yer olmaya bilərdi. Qərar: pause = resurs saxlanır.

**12. `/proc`-dan nə oxuyursan və necə interpret edirsən?**
`stat`: state, ppid, pgrp, utime/stime/cutime/cstime, nice, threads, starttime, rss. `status`: context switch-lər, `Cpus_allowed_list`. `/proc/stat`: sistem CPU.
Interpretasiya: CPU % = Δcpu-vaxt/Δdivar-vaxtı; efficiency = CPU%/(100×verilən core); **CPU-bound** ≥ 70 %, **I/O-bound** (`D` state və ya
efficiency < 30 % + context switch-lərin ≥ 70 %-i könüllü), **STOPPED** (hamısı `T`). Detal: `stat`-ın 2-ci sahəsi (`comm`) mötərizədədir
və boşluq/`)` ola bilər - ona görə **son** `)` simvolundan bölürəm (unit test: `4242 (my (weird) prog)`).

**13. Yaddaş enforcement necə işləyir? Zəifliyi?**
ratio = RSS/declared; ≤ 1.25 OK, ≤ 2 WARNING, > 2 VIOLATION; 3 **ardıcıl** VIOLATION ölçməsindən sonra action (terminate/pause/renice/log) - tək spike-ı bağışlayır.
Zəiflik: polling (0.5 s) - sürətli allocator ölçmələr arasında aşa bilər; hard limit üçün cgroup `memory.max` lazımdır (implementasiya olunmayıb).
RSS paylaşılan səhifələri təkrar sayır (konservativ).

**14. Scheduler crash edərsə?**
Vəziyyət SQLite-da (WAL, atomik). Restartda `recover()`: PID sağdırsa **və** `/proc` start time-ı saxlanmışla üst-üstə düşürsə → re-attach (PID reuse-dan qorunma);
PID yoxdur + `.exit` faylı var → exit kodu oxunur; yoxdur → `FAILED: lost`; WAITING job-lar növbəyə qayıdır. Test: `kill -9` + restart.

**15. Niyə SQLite, JSON/CSV yox?**
Atomik yazı (crash-safe), sorğular (`history`, `stats`, `why`), stdlib. JSON/CSV üçün əl ilə atomic-rename və lock lazım olardı.

**16. Niyə WSL2? VM tələb olunur.**
WSL2 real Linux kernel-dir; `/proc`, siqnallar, `sched_setaffinity`, `nice` eynidir. Kod yalnız stdlib + `/proc` ilə portativdir. `scripts/setup_wsl2.sh` mühiti yoxlayır.
Müəllim VM tələb edərsə, repo-nu klonlayıb `make test` ilə eyni nəticəni göstərmək olar.

**17. Host-da 4 CPU yoxdursa?**
Scheduler *virtual* 4 CPU idarə edir (`--cpus`). Məntiqi core `i` real core `i mod N`-ə düşür. Qərar məntiqi və siqnallar dəyişmir; yalnız real CPU-bound
job-lar fiziki core-ları bölüşür (`cpu_bound` eksperimentində ETA xətası 1.4-3.1 s - bunun səbəbidir).

**18. SJF makespan-ı niyə pisləşdirdi?**
SJF *uzunluğa* görə sıralayır, *ölçüyə* yox. `memory` ssenarisində iki böyük job (2500 MB və 3000 MB, cəmi > 4 GB) sona qaldı və ardıcıl işlədi
(2.3 → 5.4 → 8.5 s); FCFS-də 2500 MB-lıq job t=0-da başlayıb başqaları ilə üst-üstə düşdü. Orta gözləmə yaxşıdır, amma toplam throughput pisdir.

**19. FCFS+backfill niyə `cpu_bound`-da kömək etdi, `memory`-də yox?**
`cpu_bound`-da geniş (4 CPU) job gözləyərkən CPU-lar boş qalırdı; backfill onları qısa job-larla doldurdu (6.84 → 3.52 s). `memory`-də baş job-un gecikməsi
çox qısa idi, arxadakı job-lar boşluğa sığmırdı. Backfill heç bir ssenaridə zərər vermədi.

**20. Innovation nədir? Kosmetik deyil?**
Öyrənən runtime estimator + ETA: SJF və backfill-in əsas zəifliyini (təxminlər pis olur) həll edir; ETA eyni `plan()` funksiyasını virtual saatla işlədir.
Əlavə: dependencies, timeout, retry. Ölçü: təxmin xətası 1829 % → 0-1 % (3-cü run).

**21. Niyə Round Robin / cgroups / thermal yoxdur?**
Round Robin optional idi; lazımi mexanizm (SIGSTOP/SIGCONT) var, kvantum timer-i əlavə olunmayıb. cgroups v2 bonus idi və inkişaf mühitində (cgroup2 yox) test edə
bilməzdim - test olunmamış kod göndərmədim. Thermal: WSL2/VM-də temperatur sensoru yoxdur (`setup_wsl2.sh` yoxlayır).

**22. Test və yoxlama?**
49 test: planner (policy-lər, backfill, aging, ETA), `/proc` parser, allocator, enforcement, estimator + real daemon və real proseslərlə integration
(siqnallar, affinity/nice-ın `/proc`-da yoxlanması, memory violation, timeout, dependency, retry, crash recovery). Testlər real bug-lar tapdı: recovery zamanı
normal bitmiş job `FAILED/lost` sayılırdı; exit vaxtı səhv hesablanırdı.

**23. Ən böyük məhdudiyyət / nəyi fərqli edərdin?**
Sərt yaddaş limiti üçün cgroup v2; təxminlər səhv olanda backfill-in davranışını ölçmək; `Round Robin`; PSS ilə dəqiq yaddaş; çox istifadəçili mühit üçün
socket autentifikasiyası.

## 5. Bilməli olduğun rəqəmlər

| | |
|---|---|
| Defense testi (əl hesabı) | FCFS orta gözləmə 45 s, SJF 1.25 s; makespan 65 s |
| Ölçülmüş (÷10 miqyas) | FCFS 4.54 s, SJF 0.27 s |
| Backfill (`cpu_bound`) | 6.84 → 3.52 s gözləmə |
| Starvation | LOW: 8.5 s (aging yox) → 4.4 s (aging) ; HIGH: 1.05 → 2.07 s |
| Estimator | 20 s təxmin → 10.5 s → 1.04 s (real 1.04 s) |
| nice çəkiləri | 0 → 1024, 5 → 335, 15 → 36 (nice 0 ≈ 28× nice 15 yük altında) |
| Enforcement hədləri | WARNING > 1.25×, VIOLATION > 2×, 3 ardıcıl ölçmə; tick 0.5 s |
| Testlər | 49 |

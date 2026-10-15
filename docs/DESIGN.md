# Design note — Never Lose a Heartbeat

*(≤ 2 pages when exported to PDF. Numbers marked ⟨…⟩ are filled in from the ARM64 runs.)*

## 1. Architecture

```
 Raspberry Pi 4 (≤4 cores, ≤2 GB)                                  Free cloud VM
 biosignal-mock ─TCP─► Collector ─200 ms commit─► SQLite (WAL, FULL)   Caddy (HTTPS)
                       CRC · dedup · gaps          chunks · losses ──► Ingest API ─► SQLite
                                                   cursors · results     Online worker
                       Health/mode (1 s) ◄──────── Uploader ─────────►  (same processing.py)
                       Offline worker (same processing.py)
```

- **Collector:** one TCP connection; polls each sensor every 100 ms and always asks for
  `from_index = cursor`. The CRC is checked on the **raw `data` text** of the received line, never
  on re-serialised JSON. Every line is applied by its own sensor and index range, so duplicate,
  late and corrupt replies are harmless.
- **Store:** SQLite in WAL mode with 1 KB pages. Samples are stored as immutable blocks of
  ≤ 400 samples (packed int32). The chunks, loss records and cursor advances read since the
  last commit are written **in one transaction every 200 ms**, with `synchronous=FULL`
  (fsync per commit).
  - A dedicated writer thread commits the batches strictly in order, so a slow fsync (SD cards
    stall) never stops the collector reading the mock's 10 s buffer.
  - Only this collector connection fsyncs. Upload and processing bookkeeping is safe to
    lose in a crash, so it does not fsync.
  - Upload progress is a few merged ranges of chunk ids, so stored chunks are never rewritten.
- **Uploader:** sends gzip JSON batches every 0.5 s. Order of priority: session ends, losses,
  offline results, the newest 30 s of data, then the backlog oldest-first. A block counts as
  uploaded only **after** the cloud has committed it and replied.
- **Restart:** systemd `Restart=always`, `RestartSec=200ms`. The collector connects to the mock
  before the cloud code is even imported. Bytecode is precompiled at install time.
- **Mode manager:** health ping every 1 s with a hard 1.5 s deadline. 3 failures → offline;
  2 successes → online.
- **Processing:** one module (`processing.py`) used unchanged by the device (offline) and the
  cloud (online). A window is computed only when all of its inputs are *final*: data, a recorded
  loss, or past the session end. A sample still in transit is therefore never counted as missing.

## 2. Why nothing is lost silently

For every (session, sensor), the index range `[0, cursor)` is covered **exactly once** by stored
blocks and recorded losses. Writes go in the same transaction as the cursor advance, so this
holds after any crash.
- Data that was received but not committed is simply "not read yet". On restart it is re-read
  from the mock, which keeps 10 s.
- An index jump the mock did not explain is re-read, not recorded. It is recorded as lost
  (`not_delivered_by_mock`) only after 5 failed re-reads.
- A new `session_id` closes the old session: its unread tail is listed with `to_index: null`.
  The reason is `device_power_loss_or_reboot` if the Linux boot id changed, else `mock_restart`.
- **Disk full, in two stages.**
  1. Below 24 MB free: release the 16 MB reserve file and **keep saving** into that space
     (≈ 25 min of data).
  2. Below 4 MB: store only tiny `disk_full` loss records while the mock keeps being read.

  When space returns, the reserve is recreated and saving resumes.

**Exactly once in the cloud:** blocks are immutable and keyed (session, sensor, first_index), and
the cloud does `INSERT OR IGNORE`. A crash between the cloud's commit and the device's ack only
causes a harmless re-send.

## 3. Guarantees

| Guaranteed | Not guaranteed |
|---|---|
| Every sample received from the mock is stored once, unchanged (CRC-checked) | Samples the mock never delivered (listed with the mock's reason) |
| Every index not stored is listed with sensor, range and reason | The last ≤ 0.5 s before a power cut (listed, not silent) |
| Process crash / `kill -9`: **zero loss** (restart in ~1 s; the mock buffers 10 s) | Storage hardware that acknowledges a flush it did not perform |
| Network switch detected ≤ ⟨5.5⟩ s (offline) / ≤ ⟨2⟩ s (online); no sample lost or duplicated | Outages longer than the free disk (≈ 30 MB/h → ⟨N⟩ days on a 32 GB card) |
| Online result ≤ 5 s after its second (measured median ⟨0.9⟩ s, max ⟨3.3⟩ s) | Online results during an outage (those windows are processed offline instead) |
| Identical results online and offline (same code, exact rational rounding) | |
| Wall-clock changes have no effect (indices order data; monotonic timers) | |

## 4. Maximum data loss on a power cut: **0.5 s per sensor**

When power is cut, the mock dies too. What is lost is what the mock had produced but we had not
yet committed:
- up to one poll interval not yet read (100 ms);
- plus one commit interval not yet flushed (200 ms);
- plus the fsync latency of the SD card (⟨measured⟩ ms).

That is ≈ 0.35 s, and we state **0.5 s** for margin. Measured worst case over ⟨5⟩ power cuts: ⟨…⟩ s.
These samples are not silent: after reboot they appear as the old session's tail loss.
Committing more often would lower the bound at the cost of more SD-card writes. 200 ms keeps disk
writes at ⟨…⟩ MB/h.

## 5. Results logic

- Windows are `400w … 400w+399`. Stats are per channel over available samples (`null` if none).
- Means use exact rationals; magnitude uses `math.fsum`; rounding is half away from zero to 1 dp.
- **Peaks:**
  - the 800 previous samples and the next sample are available;
  - the sample is a strict local maximum;
  - `2·x > max(previous 800)`;
  - it is ≥ 100 samples after the previous peak.
- **Rolling max:** a monotonic deque, O(n).
- **HR** = 24000·(k−1)/(last−first) over the peaks in the 5 s span, computed exactly.
- The previous-peak chain starts 2000 samples before the span. This equals the full-history
  chain whenever candidate peaks are ever ≥ 100 apart (always true for ECG); a unit test checks
  it against the literal full-history rules.

## 6. Resources (1 h online, ⟨environment⟩)

| Avg CPU | Peak memory | Disk written | Network sent |
|---|---|---|---|
| ⟨…⟩ % | ⟨…⟩ MB | ⟨…⟩ MB | ⟨…⟩ MB |

## 7. Assumptions (asked of the organisers)

1. The peak chain carries across the whole session.
2. Field names `mean_1/min_1/max_1/mean_2/…` are per-channel lists.
3. Rounding is half away from zero.
4. Windows processed offline are not re-processed online.

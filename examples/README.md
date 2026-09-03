# 🧪 SluM prover examples

These examples download and build real theorem provers, generate readable SluM
submission files, and provide Makefile targets for submitting and monitoring
the resulting Slurm jobs.

For installation, custom solver descriptions, and the full command reference,
see the [main SluM documentation](../README.md).

<a id="available-examples"></a>

<details>
<summary><strong>📚 Available examples</strong> <a href="#available-examples" title="Permalink to Available examples">🔗</a></summary>

| Directory | What `make` obtains and builds |
| --- | --- |
| [`example-vampire`](example-vampire/) | The latest Vampire source from its [official Git repository](https://github.com/vprover/vampire), including the CaDiCaL and VIRAS submodules; built as a static CMake release binary for cluster portability |
| [`example-e`](example-e/) | The latest E source from its [official Git repository](https://github.com/eprover/eprover); configured and rebuilt as a first-order prover |
| [`example-drodi`](example-drodi/) | Drodi 4.1.1 from its [official CASC-J13 source archive](https://tptp.org/CASC/J13/SystemSources/Drodi---4.1.1.tgz); the archive is checked against the SHA-256 recorded in the Makefile before compilation |
| [`example-combined`](example-combined/) | All three provers in one submission, run on five easy and five hard problems with a three-second CPU limit per call |

</details>

<a id="single-prover-workflow"></a>

<details>
<summary><strong>🚀 Single-prover workflow</strong> <a href="#single-prover-workflow" title="Permalink to Single-prover workflow">🔗</a></summary>

<blockquote>

Each single-prover example contains the same three real TPTP problems and a
Makefile. The examples do not contain checked-in prover binaries or source
trees. On the first run, `make` downloads and builds the prover, builds
`runsolver` if necessary, and calls `slum.py` to create `submit.sh` and
`submit.sh.files/`.

From the repository root, build, inspect, and submit Vampire with:

```bash
cd examples/example-vampire
make
less submit.sh
less submit.sh.files/slurm_job.sh
less submit.sh.files/batches/batch_000000.sh
make submit
make monitor
```

Use `examples/example-e` or `examples/example-drodi` in the first command to
run the corresponding example.

<a id="makefile-targets"></a>

<details>
<summary><strong>🛠️ Makefile targets</strong> <a href="#makefile-targets" title="Permalink to Makefile targets">🔗</a></summary>

The single-prover Makefiles provide the same targets:

| Command | Effect |
| --- | --- |
| `make` or `make all` | Build the prover and `runsolver` if needed, then generate the submission files |
| `make build` | Build the prover and `runsolver` without generating a submission |
| `make submit` | Run the generated `submit.sh` and return after Slurm accepts the job array |
| `make monitor` | Open the SluM dashboard for `slurmy` |
| `make clean` | Remove only `submit.sh` and `submit.sh.files/` |
| `make distclean` | Also remove that example's downloaded source and built prover binary |

</details>

<a id="expected-results"></a>

<details>
<summary><strong>✅ Expected results</strong> <a href="#expected-results" title="Permalink to Expected results">🔗</a></summary>

Connect to the TU Wien VPN before running `submit.sh` if `slurmy` is not
reachable directly. Each single-prover example submits two array elements to
`CPU-amd`. Vampire and E should quickly report the following expected statuses;
Drodi also proves the first two within the example limit but may time out on
the satisfiable problem:

```text
PUZ001+1.p  Theorem
ALG002-1.p  Unsatisfiable
ALG299-1.p  Satisfiable
```

</details>

</blockquote>

</details>

<a id="combined-example"></a>

<details>
<summary><strong>🔬 Combined example</strong> <a href="#combined-example" title="Permalink to Combined example">🔗</a></summary>

The combined example exercises multiple solver descriptions in one SluM job.
Its `problems/easy/` directory contains five low-rated TPTP problems, while
`problems/hard/` contains five problems taken from the locally curated hard
benchmark set. All ten are self-contained FOF or CNF files with no external
axiom includes.

```text
easy: ALG002-1, PUZ001+1, PUZ002-1, PUZ003-1, PUZ004-1
hard: MPT0554+1, MPT1048+1, MPT1388+1, MPT1787+1, MPT1887+1
```

Running `make` builds the three single-prover examples and generates 30 calls:
each of Vampire, E, and Drodi on every problem. The calls are divided among six
Slurm array tasks, with a three-second CPU limit and six-second wall-clock
limit per prover call.

From the repository root:

```bash
cd examples/example-combined
make
make submit
make sync
```

`make sync` follows the latest remote SluM job until it completes and stores
the incrementally downloaded results under `slum-results/` at the repository
root. To avoid relying on which remote job is newest, pass the SluM ID printed
by `make submit` explicitly:

```bash
make sync SLUM_ID=alice_1786464000_12345
```

Use `make monitor` instead when you want to watch the same run interactively.
`make distclean` removes the generated submission together with the source
trees and binaries shared with the three single-prover examples.

The expected three-second test pattern is that every prover solves the easy
set, while the hard set produces a mixture of solutions and ordinary time
limits. Exact hard-problem results can change when the Makefiles fetch newer
Vampire or E revisions.

</details>

# Team Ngeebula's SMRT Railway Track Access Scheduler Optimisation

## 1. Overview: What the Solution Is
The **SMRT Railway Track Access Scheduler Optimisation** is a high-performance optimization and scheduling engine built to decide who gets access to repair the track, on which nights, for the dual-line rail network (Line Alpha & Line Beta). The system handles contested nightly track windows, spatial sector allocations, and strict safety buffers to generate concrete, dispatchable possession schedules (`SCHEDULE_ACCESS.csv`, `SCHEDULE_OCCUPANCY.csv`, and `RESULTS.csv`). The repository includes an interactive **Streamlit** dashboard, which is run using **Google Cloud Platform**, for execution and an automated local validation pipeline.


## 2. Solution Architecture
The core optimization engine follows a rigorous, multi-stage architecture powered by Google OR-Tools CP-SAT:

* **1. Ingestion & Parsing**
  * Loads the 8 core configuration and data CSV files (`01_LINES.csv`, `02_STATIONS.csv`, `03_SECTORS.csv`, `04_LOCATION_SUPPLY.csv`, `05_BUFFER_LOCATION.csv`, `06_PARAMETERS.csv`, `07_PROJECT_DETAILS.csv`, `08_ACTIVITY_DETAILS.csv`), cleaning data types and headers, mapping location aliases, and establishing scheduling horizons based on the horizon_start and horizon_week keys in the dictionary listed in `06_PARAMETERS.csv`.
* **2. CP-SAT Variable Definition**
  * Formulates binary possession allocation variables ($x[a, w, d]$ for activity $a$, week $w$, night $d$), Early Closure/Late Opening (ECLO) toggle indicators ($eclo[a, w]$ where 0 = Standard, 1 = Early Closure), slot seating, and overrun/excess metrics.
* **3. Hard Constraint Matrix**
  * **Workload Completion:** Mandates that 100% of activities in `08_ACTIVITY_DETAILS.csv` are scheduled with nightly yields summing to $\ge \text{total\_accesses}$.
  * **Legal Mixes & Capacity:** Enforces location pack limits (one PM alone, or one PC + $\le 3$ C, or $\le 4$ C per spot) and spatial safety buffers (non-co-sharing items carry exclusion zones; buffers never overlap).
  * **Live 750V Mirroring & Interchange Rules:** Third-rail power cuts mirror to the opposite bound ($EB \leftrightarrow WB$) and cross over to the adjacent line's interchange tunnel/platforms at Hubs H01 and H02.
  * **Weekly Allocation & Workfronts:** Respects flat per-contract weekly caps (`number_of_maximum_access_per_week` — 2 for Live, 3 for others) and concurrent workfront limits.
  * **Predecessor Precedence:** Enforces that successor activities start strictly after predecessor completion.
* **4. Scenario Objective Switchboard**
  * **Scenario A (Strict Supply, Flexible Schedule):** Track capacity limits are rigid (zero excess capacity permitted) and ECLO is hard-forbidden. Optimizes to minimize priority-weighted project completion overruns (Priority 1 > 2 > 3).
  * **Scenario B (Strict Schedule, Flexible Supply):** Planned completion dates are rigid (zero overrun permitted). Optimizes to minimize additional access-nights above nominal supply ($7 \times \text{excess}$) plus ECLO penalties ($5 \times \text{eclo}$).
  * **Scenario C (Elastic Trade-Off):** Combines priority-weighted overruns, excess access-nights (with a narrow allowance up to 1 excess per location-week before hard-failing), and ECLO penalties within a 2-week continuous window.
* **5. Solver Execution & Export**
  * Executes the underlying CP-SAT optimization engine to return optimal or feasible status results, automatically formatting and exporting the required output files.

---

## 3. Features, Functions, & How It Works
The application integrates the solver pipeline into a streamlined, user-facing workflow:

### Core Features & Functions
* **Interactive Dashboard UI:** Built via Streamlit to provide an accessible graphical interface for managing inputs and triggering workflows without command-line overhead.
* **Multi-Scenario Configuration Panel:** Allows users to easily switch between Scenarios A, B, and C to tailor objective parameters and constraints accordingly.
* **Automated Output Verification:** Integrates direct execution of the local validator (`app/validator.py`) within the app workflow to test outputs instantly for zero hard violations and inspect soft penalty scores.
* **Downloadable Results:** Generates correctly formatted output files ready for official portal submission.

### How It Works (End-to-End Workflow)
1. **Data Ingestion:** The user loads or uploads the scenario input files via the Streamlit interface.
2. **Execution & Optimization:** Clicking "Run Solver" triggers the core optimization script, applying the CP-SAT models and constraint matrices to build an optimal schedule.
3. **Local Validation Check:** The system automatically passes generated outputs through the validation module to check for hard constraint breaches and compute objective scores. 
4. **Final Export:** Verified schedules are exported and locked in for portal submission, preserving precious upload slots.

---

## 4. Google Cloud Platform (GCP) Deployment
To meet the requirement of running the Streamlit application using Google Cloud resources:

### Containerization (Docker)
Create a `Dockerfile` in your root directory to package the Streamlit app:
```dockerfile
FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8501
CMD ["streamlit", "run", "streamlit_app.py", "--server.port=8501", "--server.address=0.0.0.0"]
```

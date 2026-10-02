"""A compact, multilingual (EN/FR/DE) vocabulary of technical terms.

It powers three things *without* needing the LLM (so it is fast and trustworthy):
  1. spotting the technologies a job posting asks for,
  2. scoring which of your projects/experiences are relevant,
  3. fact-checking generated text (a tool in the letter that is not in your profile is flagged).

Line format:   canonical | Display name | group | alias; alias; ...
Alias prefixes: ``cs:`` case-sensitive,  ``re:`` raw regular expression (no word-boundary wrapper added).
Add your own terms freely - the format is intentionally simple.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

_SRC = r"""
# ---------------------------------------------------------------- programming languages
python|Python|lang|python; python3; python 3; pythonic
java|Java|lang|cs:Java; jvm; jdk; openjdk
javascript|JavaScript|lang|javascript; ecmascript; cs:JS; es6
typescript|TypeScript|lang|typescript
c++|C++|lang|c++; cpp; c plus plus
c#|C#|lang|c#; csharp; c sharp
rust|Rust|lang|rust
kotlin|Kotlin|lang|kotlin
swift|Swift|lang|cs:Swift
php|PHP|lang|php
ruby|Ruby|lang|ruby
scala|Scala|lang|scala
perl|Perl|lang|perl
lua|Lua|lang|lua
haskell|Haskell|lang|haskell
ocaml|OCaml|lang|ocaml
elixir|Elixir|lang|elixir
dart|Dart|lang|cs:Dart
matlab|MATLAB|lang|matlab; simulink
bash|Bash / Shell|lang|bash; shell scripting; shell scripts; shell script; scripts shell; scripting shell; zsh
powershell|PowerShell|lang|powershell
sql|SQL|data|sql; t-sql; pl/sql; plsql; pl-sql
vhdl|VHDL/Verilog|embedded|vhdl; verilog
solidity|Solidity|lang|solidity
# go / r / c are matched by special rules in code (too ambiguous for plain aliases)
go|Go|lang|golang
r|R|lang|rstudio; r language; r programming
c|C|lang|c language; c programming; langage c

# ---------------------------------------------------------------- web / frontend
react|React|web|re:\bReact(?!\s+Native)\b; react.js; reactjs
angular|Angular|web|angular; angularjs
vue|Vue.js|web|re:\bVue(?!\s+d[e’'])\b; vue.js; vuejs; nuxt; nuxt.js
svelte|Svelte|web|svelte; sveltekit
next.js|Next.js|web|next.js; nextjs
html|HTML|web|html; html5
css|CSS|web|css; css3; sass; scss
tailwind|Tailwind CSS|web|tailwind; tailwindcss; tailwind css
bootstrap|Bootstrap|web|bootstrap
jquery|jQuery|web|jquery
redux|Redux|web|redux; rxjs
webpack|Webpack/Vite|web|webpack; vite
graphql|GraphQL|backend|graphql
websocket|WebSockets|backend|websocket; websockets
htmx|HTMX|web|htmx
figma|Figma|web|figma
electron|Electron|web|electron

# ---------------------------------------------------------------- backend frameworks & concepts
node.js|Node.js|backend|node.js; nodejs; node js
express|Express|backend|express.js; expressjs; express js
nestjs|NestJS|backend|nestjs; nest.js
fastapi|FastAPI|backend|fastapi
flask|Flask|backend|flask
django|Django|backend|django
spring|Spring|backend|spring boot; springboot; spring framework; spring cloud; spring mvc; spring data
hibernate|Hibernate/JPA|backend|hibernate; jpa
quarkus|Quarkus|backend|quarkus; micronaut
laravel|Laravel|backend|laravel
symfony|Symfony|backend|symfony
rails|Ruby on Rails|backend|ruby on rails; cs:Rails
.net|.NET|backend|.net; dotnet; .net core; asp.net; asp.net core; blazor
wordpress|WordPress|backend|wordpress; drupal; prestashop; magento
rest|REST APIs|backend|rest api; rest apis; restful; api rest; apis rest; api restful; restful api
grpc|gRPC|backend|grpc; protobuf; protocol buffers
microservices|Microservices|backend|microservices; microservice; micro-services; micro services; microservices architecture
serverless|Serverless|cloud|serverless; faas
event-driven|Event-driven architecture|backend|event-driven; event driven; architecture événementielle; event sourcing; cqrs
message-queue|Message queues|backend|message queue; message queues; file de messages; message broker
oauth|OAuth / SSO|security|oauth; oauth2; openid connect; oidc; keycloak; sso; jwt

# ---------------------------------------------------------------- data
postgresql|PostgreSQL|data|postgresql; postgres; psql; pgsql; postgis
mysql|MySQL / MariaDB|data|mysql; mariadb
sqlite|SQLite|data|sqlite
mongodb|MongoDB|data|mongodb; mongo db
nosql|NoSQL|data|nosql
redis|Redis|data|redis
cassandra|Cassandra|data|cassandra; scylladb
neo4j|Neo4j|data|neo4j; graph database; base de données graphe
elasticsearch|Elasticsearch|data|elasticsearch; opensearch; elastic search
elk|ELK stack|sre|elk; elk stack; elastic stack; logstash; kibana
dynamodb|DynamoDB|data|dynamodb
influxdb|InfluxDB / TimescaleDB|data|influxdb; timescaledb
clickhouse|ClickHouse|data|clickhouse
snowflake|Snowflake|data|snowflake
bigquery|BigQuery|data|bigquery
redshift|Redshift|data|redshift
databricks|Databricks|data|databricks
dbt|dbt|data|dbt
airflow|Apache Airflow|data|airflow; dagster; prefect
kafka|Apache Kafka|data|kafka; redpanda
rabbitmq|RabbitMQ|data|rabbitmq; activemq
spark|Apache Spark|data|apache spark; pyspark; spark sql; spark streaming; cs:Spark
flink|Apache Flink|data|flink
hadoop|Hadoop|data|hadoop; hdfs; hive
etl|ETL / data pipelines|data|etl; elt; data pipeline; data pipelines; pipelines de données
power bi|Power BI / Tableau|data|power bi; powerbi; tableau; looker; metabase; superset
data engineering|Data engineering|data|data engineering; data engineer; ingénierie des données
data analysis|Data analysis|data|data analysis; data analytics; analyse de données; datenanalyse
oracle|Oracle DB|data|oracle db; oracle database
orm|ORM|backend|orm; sqlalchemy; prisma; typeorm; sequelize

# ---------------------------------------------------------------- cloud
aws|AWS|cloud|aws; amazon web services; ec2; s3; aws lambda; cloudwatch; rds; sqs; cloudfront; fargate; ecs
azure|Azure|cloud|azure; microsoft azure
gcp|Google Cloud|cloud|gcp; google cloud; google cloud platform; cloud run
openstack|OpenStack|cloud|openstack
vmware|VMware / Proxmox|cloud|vmware; vsphere; proxmox; esxi; virtualisation; virtualization
cloudformation|CloudFormation|cloud|cloudformation; aws cdk; pulumi; bicep
cloud-native|Cloud-native|cloud|cloud native; cloud-native; cloud computing

# ---------------------------------------------------------------- devops
docker|Docker|devops|docker; dockerfile; docker compose; docker-compose; dockerized; dockerisé; podman
containers|Containers|devops|containers; containerization; containerisation; conteneurs; conteneurisation; containerisierung
kubernetes|Kubernetes|devops|kubernetes; k8s; k3s; k3d; minikube; eks; aks; gke
openshift|OpenShift / Rancher|devops|openshift; okd; rancher
helm|Helm|devops|cs:Helm; kustomize
terraform|Terraform|devops|terraform; opentofu
ansible|Ansible|devops|ansible
iac|Infrastructure as Code|devops|iac; infrastructure as code; infrastructure-as-code; infrastructure en tant que code
ci/cd|CI/CD|devops|ci/cd; ci-cd; cicd; ci cd; continuous integration; continuous delivery; continuous deployment; intégration continue; déploiement continu; livraison continue; kontinuierliche integration; pipelines ci
gitlab ci|GitLab CI|devops|gitlab ci; gitlab-ci; gitlab ci/cd; gitlab pipelines
github actions|GitHub Actions|devops|github actions; gh actions
jenkins|Jenkins|devops|jenkins; circleci; teamcity; travis ci; azure pipelines
argocd|Argo CD / Flux|devops|argo cd; argocd; argo-cd; fluxcd; flux cd
gitops|GitOps|devops|gitops
devops|DevOps|devops|devops; dev ops; devsecops
platform engineering|Platform engineering|devops|platform engineering; internal developer platform; ingénierie de plateforme
git|Git|tools|git; version control; gestion de versions; versionsverwaltung
github|GitHub|tools|github
gitlab|GitLab|tools|gitlab
bitbucket|Bitbucket|tools|bitbucket
nginx|Nginx / HAProxy|devops|nginx; haproxy; traefik; apache httpd; apache http server
istio|Service mesh|devops|istio; linkerd; envoy; service mesh
vault|HashiCorp Vault|security|hashicorp vault; vault secrets
consul|HashiCorp Consul|devops|consul

# ---------------------------------------------------------------- SRE / observability
sre|SRE|sre|sre; site reliability; site reliability engineering; site reliability engineer
monitoring|Monitoring|sre|monitoring; monitorage; überwachung
observability|Observability|sre|observability; observabilité; beobachtbarkeit
prometheus|Prometheus|sre|prometheus; alertmanager
grafana|Grafana|sre|grafana; loki
opentelemetry|OpenTelemetry|sre|opentelemetry; otel; open telemetry; jaeger; zipkin; tracing distribué; distributed tracing
datadog|Datadog / Splunk|sre|datadog; splunk; new relic; newrelic; dynatrace
sentry|Sentry|sre|sentry
nagios|Nagios / Zabbix|sre|nagios; zabbix; centreon; icinga
incident|Incident response|sre|incident response; incident management; gestion des incidents; on-call; astreinte; post-mortem; postmortem
slo|SLOs / SLIs|sre|slo; slos; sli; slis; error budget; service level objective
reliability|Reliability engineering|sre|reliability; fiabilité; zuverlässigkeit; high availability; haute disponibilité; hochverfügbarkeit
chaos|Chaos engineering|sre|chaos engineering; chaos monkey; litmus
performance|Performance & load testing|sre|load testing; performance testing; tests de charge; k6; jmeter; locust; gatling
automation|Automation|devops|automation; automatisation; automatisierung; scripting

# ---------------------------------------------------------------- AI / ML
ai|AI|ai|re:\bAI\b; re:\bIA\b; artificial intelligence; intelligence artificielle; künstliche intelligenz; kuenstliche intelligenz
machine learning|Machine learning|ai|machine learning; apprentissage automatique; maschinelles lernen; re:\bML\b
deep learning|Deep learning|ai|deep learning; deep-learning; apprentissage profond; neural network; neural networks; réseaux de neurones; neuronale netze
pytorch|PyTorch|ai|pytorch; torch
tensorflow|TensorFlow / Keras|ai|tensorflow; keras
scikit-learn|scikit-learn|ai|scikit-learn; sklearn; scikit learn
pandas|pandas / NumPy|ai|pandas; numpy; scipy
matplotlib|Matplotlib / Seaborn|ai|matplotlib; seaborn; plotly
jupyter|Jupyter|ai|jupyter; notebooks jupyter; colab
huggingface|Hugging Face|ai|hugging face; huggingface; transformers; cs:BERT
llm|LLMs|ai|llm; llms; large language model; large language models; grand modèle de langage; grands modèles de langage; sprachmodell; gpt; chatgpt; openai; anthropic; mistral ai; llama 3; llama3; gemini api
rag|RAG|ai|rag; retrieval augmented generation; retrieval-augmented generation; génération augmentée par la récupération
langchain|LangChain / LlamaIndex|ai|langchain; llamaindex; llama index; langgraph; semantic kernel
ollama|Ollama / llama.cpp|ai|ollama; llama.cpp; vllm; gguf
prompt engineering|Prompt engineering|ai|prompt engineering; prompting
fine-tuning|Fine-tuning|ai|fine-tuning; finetuning; fine tuning; lora; qlora; peft; rlhf
agents|AI agents|ai|ai agents; agents ia; agentic; agentic workflows; multi-agent; mcp server; model context protocol
mlops|MLOps|ai|mlops; mlflow; kubeflow; dvc; weights & biases; wandb; ml pipeline; ml pipelines
onnx|ONNX / TensorRT|ai|onnx; tensorrt; openvino; tflite
cuda|CUDA / GPU|ai|cuda; gpu; gpus
nlp|NLP|ai|nlp; natural language processing; traitement du langage naturel; traitement automatique des langues; spacy; nltk
computer vision|Computer vision|ai|computer vision; vision par ordinateur; opencv; yolo; object detection; détection d'objets; image segmentation; bildverarbeitung
reinforcement learning|Reinforcement learning|ai|reinforcement learning; apprentissage par renforcement
generative ai|Generative AI|ai|generative ai; ia générative; genai; gen ai; generative models; stable diffusion; diffusion models; generative ki
speech|Speech / audio ML|ai|speech recognition; reconnaissance vocale; whisper; text-to-speech; tts; asr
vector database|Vector databases|ai|vector database; vector databases; base de données vectorielle; bases de données vectorielles; faiss; pinecone; qdrant; weaviate; milvus; chromadb; pgvector; embeddings; embedding
time series|Time series|ai|time series; séries temporelles; zeitreihen; forecasting; prévision
recommender|Recommender systems|ai|recommender; recommender systems; systèmes de recommandation; recommendation engine
data science|Data science|ai|data science; data scientist; science des données

# ---------------------------------------------------------------- testing
pytest|pytest|testing|pytest; unittest; nose
junit|JUnit|testing|junit; testng; mockito
jest|Jest / Mocha|testing|cs:Jest; mocha; vitest
cypress|Cypress / Playwright|testing|cypress; playwright; puppeteer
selenium|Selenium|testing|selenium; webdriver
postman|Postman|testing|postman; insomnia; swagger; openapi
testing|Software testing|testing|unit tests; unit testing; tests unitaires; integration tests; tests d'intégration; end-to-end tests; e2e; tdd; bdd; test-driven; behaviour-driven; cucumber
sonarqube|SonarQube|testing|sonarqube; sonar; code quality; qualité de code

# ---------------------------------------------------------------- mobile
android|Android|mobile|android; jetpack compose
ios|iOS|mobile|cs:iOS; swiftui; xcode; uikit
flutter|Flutter|mobile|flutter
react native|React Native|mobile|react native; ionic; xamarin; kotlin multiplatform

# ---------------------------------------------------------------- security
security|Cybersecurity|security|cybersecurity; cybersécurité; cybersicherheit; sécurité informatique; it security; information security; appsec; application security
owasp|OWASP|security|owasp; sast; dast; snyk; trivy
pentest|Penetration testing|security|pentest; penetration testing; tests d'intrusion; pen test; red team; ethical hacking; burp suite; metasploit
siem|SIEM / SOC|security|siem; soc; edr; threat detection; threat hunting; wazuh
cryptography|Cryptography|security|cryptography; cryptographie; kryptographie; pki; tls; ssl; certificates
iam|IAM|security|iam; identity and access management; active directory; ldap

# ---------------------------------------------------------------- embedded / IoT
embedded|Embedded systems|embedded|embedded; embedded systems; embarqué; systèmes embarqués; eingebettete systeme; firmware; microcontroller; microcontrôleur; mikrocontroller
stm32|STM32 / Arduino|embedded|stm32; arduino; esp32; raspberry pi; nxp
rtos|RTOS|embedded|rtos; freertos; zephyr; yocto; buildroot; embedded linux
iot|IoT|embedded|iot; internet of things; mqtt; lorawan; zigbee; cs:BLE
ros|ROS / robotics|embedded|ros; ros2; robotics; robotique; robotik
can|CAN / automotive|embedded|can bus; autosar; automotive; automobile
fpga|FPGA|embedded|fpga; asic

# ---------------------------------------------------------------- OS / network
linux|Linux|os|linux; gnu/linux; ubuntu; debian; centos; rhel; red hat; fedora; alpine linux; unix; freebsd
windows server|Windows Server|os|windows server; wsl
networking|Networking|network|tcp/ip; dns; dhcp; vpn; bgp; ospf; vlan; cisco; firewall; load balancing; load balancer; réseaux; netzwerk; wireshark; sdn
http|HTTP|network|http; https; http/2; http2

# ---------------------------------------------------------------- practices
agile|Agile / Scrum|method|agile; scrum; kanban; sprint; sprints
jira|Jira / Confluence|method|jira; confluence; trello
design patterns|Design patterns|method|design patterns; clean architecture; ddd; domain-driven design; hexagonal architecture; solid principles; patterns de conception
oop|Object-oriented programming|method|oop; object-oriented; object oriented; programmation orientée objet; objektorientierte programmierung; cs:POO
functional|Functional programming|method|functional programming; programmation fonctionnelle; funktionale programmierung
algorithms|Algorithms & data structures|method|algorithms; algorithmique; algorithmen; data structures; structures de données; datenstrukturen
open source|Open source|method|open source; open-source; opensource
"""


@dataclass(frozen=True)
class Term:
    canonical: str
    display: str
    group: str
    patterns: tuple[re.Pattern, ...]


def _wrap(alias: str, cs: bool) -> re.Pattern:
    esc = re.escape(alias).replace(r"\ ", r"[\s\-]?")
    pat = rf"(?<![\w]){esc}(?![\w])"
    return re.compile(pat, 0 if cs else re.IGNORECASE)


def _build() -> dict[str, Term]:
    terms: dict[str, Term] = {}
    for raw in _SRC.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        canonical, display, group, aliases = [p.strip() for p in line.split("|", 3)]
        pats: list[re.Pattern] = []
        for a in [x.strip() for x in aliases.split(";") if x.strip()]:
            if a.startswith("re:"):
                pats.append(re.compile(a[3:]))
            elif a.startswith("cs:"):
                pats.append(_wrap(a[3:], True))
            else:
                pats.append(_wrap(a, False))
        terms[canonical] = Term(canonical, display, group, tuple(pats))
    return terms


TERMS: dict[str, Term] = _build()

# --- special rules for ambiguous single-letter / common-word languages --------------------------
_LIST_PUNCT = ",;/|•·()[]:"
_GO_CTX_AFTER = re.compile(
    r"^\s*(?:developer|developers|programming|language|backend|back-end|services?|microservices?|"
    r"ecosystem|routines?|modules?|projects?|code|stack|d[ée]veloppeur|d[ée]veloppement)\b",
    re.IGNORECASE,
)


def _count_listlike(text: str, token: str) -> int:
    """Count ``token`` (case-sensitive) when it sits in a list context: 'Python, Go, Rust' / 'Go/Rust'."""
    n = 0
    for m in re.finditer(rf"(?<![\w.+#-]){re.escape(token)}(?![\w+#-])", text):
        before = text[: m.start()].rstrip(" \t")
        after = text[m.end():]
        prev_ch = before[-1:] if before else "\n"
        next_ch = after.lstrip(" \t")[:1] or "\n"
        # "R" / "C" / "Go" surrounded by list punctuation or line edges, e.g. "Python, R, SQL"
        if (prev_ch in _LIST_PUNCT or prev_ch == "\n" or prev_ch == "-") and (next_ch in _LIST_PUNCT or next_ch == "\n"):
            n += 1
        elif token == "Go" and _GO_CTX_AFTER.match(after):
            n += 1
    return n


def _special_counts(text: str) -> Counter:
    c: Counter = Counter()
    n = _count_listlike(text, "Go")
    if n:
        c["go"] += n
    n = _count_listlike(text, "R")
    if n:
        c["r"] += n
    n = _count_listlike(text, "C")
    if n:
        c["c"] += n
    if re.search(r"(?<![\w])C/C\+\+(?![\w])", text):
        c["c"] += 1
        c["c++"] += 1
    return c


def find_terms(text: str) -> Counter:
    """Return {canonical_term: occurrences} found in ``text``."""
    found: Counter = Counter()
    if not text:
        return found
    for canon, term in TERMS.items():
        n = sum(len(p.findall(text)) for p in term.patterns)
        if n:
            found[canon] += n
    for k, v in _special_counts(text).items():
        found[k] += v
    return found


def display(canonical: str) -> str:
    t = TERMS.get(canonical)
    return t.display if t else canonical


def group_of(canonical: str) -> str:
    t = TERMS.get(canonical)
    return t.group if t else "other"


# domain -> groups whose terms characterise it (used for spontaneous applications / CV focus)
DOMAIN_GROUPS: dict[str, tuple[str, ...]] = {
    "devops": ("devops", "cloud", "os", "tools"),
    "sre": ("sre", "devops", "cloud", "os", "network"),
    "cloud": ("cloud", "devops"),
    "backend": ("backend", "data"),
    "frontend": ("web",),
    "fullstack": ("web", "backend"),
    "ai_ml": ("ai", "data"),
    "data": ("data", "ai"),
    "security": ("security", "network"),
    "mobile": ("mobile",),
    "embedded": ("embedded", "os"),
    "other": (),
}

DOMAIN_LABELS: dict[str, dict[str, str]] = {
    "devops": {"en": "DevOps", "fr": "DevOps"},
    "sre": {"en": "Site Reliability Engineering", "fr": "SRE (fiabilité des systèmes)"},
    "cloud": {"en": "Cloud engineering", "fr": "Ingénierie cloud"},
    "backend": {"en": "Backend development", "fr": "Développement backend"},
    "frontend": {"en": "Frontend development", "fr": "Développement frontend"},
    "fullstack": {"en": "Full-stack development", "fr": "Développement full-stack"},
    "ai_ml": {"en": "AI / Machine Learning", "fr": "IA / Machine Learning"},
    "data": {"en": "Data engineering", "fr": "Ingénierie des données"},
    "security": {"en": "Cybersecurity", "fr": "Cybersécurité"},
    "mobile": {"en": "Mobile development", "fr": "Développement mobile"},
    "embedded": {"en": "Embedded systems", "fr": "Systèmes embarqués"},
    "other": {"en": "Software engineering", "fr": "Ingénierie logicielle"},
}


def domain_terms(domain: str) -> set[str]:
    groups = DOMAIN_GROUPS.get(domain, ())
    return {c for c, t in TERMS.items() if t.group in groups}


def infer_domains(counter: Counter, top: int = 3) -> list[str]:
    """Guess the job's domains from the terms found in its text."""
    scores: Counter = Counter()
    for dom, groups in DOMAIN_GROUPS.items():
        if not groups:
            continue
        for term, n in counter.items():
            if group_of(term) in groups:
                # core group (first) weighs double
                scores[dom] += n * (2 if group_of(term) == groups[0] else 1)
    return [d for d, s in scores.most_common(top) if s >= 2]


# short labels for one-line headlines
DOMAIN_SHORT: dict[str, dict[str, str]] = {
    "devops": {"en": "DevOps", "fr": "DevOps"},
    "sre": {"en": "SRE", "fr": "SRE"},
    "cloud": {"en": "Cloud", "fr": "Cloud"},
    "backend": {"en": "Backend", "fr": "Backend"},
    "frontend": {"en": "Frontend", "fr": "Frontend"},
    "fullstack": {"en": "Full-stack", "fr": "Full-stack"},
    "ai_ml": {"en": "AI / ML", "fr": "IA / ML"},
    "data": {"en": "Data", "fr": "Data"},
    "security": {"en": "Security", "fr": "Cybersécurité"},
    "mobile": {"en": "Mobile", "fr": "Mobile"},
    "embedded": {"en": "Embedded", "fr": "Systèmes embarqués"},
}

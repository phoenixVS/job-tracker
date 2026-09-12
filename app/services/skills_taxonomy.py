"""Canonical technical skills and the aliases that identify them in free text.

Alias syntax:
  plain alias  -> case-insensitive match anywhere in the text
  "=Alias"     -> case-sensitive match
  "!Alias"     -> only counted inside a Skills section (ambiguous English words: Go, C, R, Spring...)
  "!=Alias"    -> both
"""

import re
from dataclasses import dataclass

SKILLS: dict[str, tuple[str, ...]] = {
    # Languages
    "Python": ("python", "python3"),
    "JavaScript": ("javascript", "js", "es6", "ecmascript"),
    "TypeScript": ("typescript", "!=TS"),
    "Java": ("java",),
    "Kotlin": ("kotlin",),
    "Scala": ("scala",),
    "Go": ("golang", "!=Go"),
    "Rust": ("rust",),
    "C": ("!=C",),
    "C++": ("c++", "cpp"),
    "C#": ("c#", "csharp"),
    "Ruby": ("ruby",),
    "PHP": ("php",),
    "Swift": ("swift",),
    "Objective-C": ("objective-c", "objective c"),
    "Dart": ("dart",),
    "Elixir": ("elixir",),
    "Erlang": ("erlang",),
    "Haskell": ("haskell",),
    "Clojure": ("clojure",),
    "R": ("!=R",),
    "Julia": ("!=Julia",),
    "Perl": ("perl",),
    "Lua": ("lua",),
    "Bash": ("bash", "shell scripting", "zsh"),
    "SQL": ("sql",),
    "GraphQL": ("graphql",),
    "HTML": ("html", "html5"),
    "CSS": ("css", "css3"),
    "Sass": ("sass", "scss"),
    "Solidity": ("solidity",),
    # Frontend
    "React": ("react", "react.js", "reactjs"),
    "Next.js": ("next.js", "nextjs"),
    "Vue.js": ("vue", "vue.js", "vuejs"),
    "Nuxt": ("nuxt", "nuxt.js"),
    "Angular": ("angular", "angularjs"),
    "Svelte": ("svelte", "sveltekit"),
    "Redux": ("redux",),
    "Tailwind CSS": ("tailwind", "tailwindcss", "tailwind css"),
    "Webpack": ("webpack",),
    "Vite": ("!=Vite",),
    "jQuery": ("jquery",),
    "React Native": ("react native",),
    "Flutter": ("flutter",),
    "SwiftUI": ("swiftui",),
    "Jetpack Compose": ("jetpack compose",),
    # Backend frameworks & runtimes
    "Node.js": ("node.js", "nodejs", "!=Node"),
    "Express": ("express.js", "expressjs", "!=Express"),
    "NestJS": ("nestjs", "nest.js"),
    "Deno": ("deno",),
    "Bun": ("!=Bun",),
    "Django": ("django",),
    "Flask": ("flask",),
    "FastAPI": ("fastapi",),
    "Celery": ("celery",),
    "Spring Boot": ("spring boot", "springboot", "!=Spring"),
    "Hibernate": ("hibernate",),
    "Ruby on Rails": ("ruby on rails", "rails"),
    "Laravel": ("laravel",),
    "Symfony": ("symfony",),
    ".NET": (".net", "dotnet", "asp.net", ".net core"),
    "Phoenix": ("!=Phoenix",),
    "gRPC": ("grpc",),
    "REST APIs": ("rest api", "rest apis", "restful", "=REST"),
    "Microservices": ("microservices", "microservice"),
    "WebSockets": ("websocket", "websockets"),
    # Data stores
    "PostgreSQL": ("postgresql", "postgres", "psql"),
    "MySQL": ("mysql",),
    "MariaDB": ("mariadb",),
    "SQLite": ("sqlite",),
    "Microsoft SQL Server": ("sql server", "mssql"),
    "Oracle Database": ("oracle db", "oracle database", "pl/sql"),
    "MongoDB": ("mongodb", "mongo"),
    "Redis": ("redis",),
    "Elasticsearch": ("elasticsearch", "elastic search", "opensearch"),
    "Cassandra": ("cassandra",),
    "DynamoDB": ("dynamodb",),
    "Neo4j": ("neo4j",),
    "ClickHouse": ("clickhouse",),
    "Snowflake": ("snowflake",),
    "BigQuery": ("bigquery",),
    "Redshift": ("redshift",),
    "Supabase": ("supabase",),
    "Firebase": ("firebase",),
    "Prisma": ("prisma",),
    "SQLAlchemy": ("sqlalchemy",),
    # Messaging & streaming
    "Kafka": ("kafka",),
    "RabbitMQ": ("rabbitmq",),
    "NATS": ("=NATS",),
    "SQS": ("=SQS",),
    "Pub/Sub": ("pub/sub", "pubsub"),
    # Cloud & infra
    "AWS": ("aws", "amazon web services"),
    "GCP": ("gcp", "google cloud"),
    "Azure": ("azure",),
    "Lambda": ("aws lambda", "!=Lambda"),
    "EC2": ("ec2",),
    "S3": ("=S3",),
    "Cloudflare": ("cloudflare",),
    "Vercel": ("vercel",),
    "Heroku": ("heroku",),
    "Docker": ("docker", "dockerfile"),
    "Kubernetes": ("kubernetes", "k8s"),
    "Helm": ("!=Helm", "helm chart", "helm charts"),
    "Terraform": ("terraform",),
    "Pulumi": ("pulumi",),
    "Ansible": ("ansible",),
    "Serverless": ("serverless",),
    "Linux": ("linux", "unix"),
    "Nginx": ("nginx",),
    "CI/CD": ("ci/cd", "continuous integration", "continuous delivery"),
    "GitHub Actions": ("github actions",),
    "GitLab CI": ("gitlab ci", "gitlab-ci"),
    "Jenkins": ("jenkins",),
    "ArgoCD": ("argocd", "argo cd"),
    "Git": ("git",),
    "Prometheus": ("prometheus",),
    "Grafana": ("grafana",),
    "Datadog": ("datadog",),
    "OpenTelemetry": ("opentelemetry",),
    "Sentry": ("sentry",),
    # Data & ML
    "Pandas": ("pandas",),
    "NumPy": ("numpy",),
    "scikit-learn": ("scikit-learn", "sklearn"),
    "PyTorch": ("pytorch",),
    "TensorFlow": ("tensorflow",),
    "Keras": ("keras",),
    "Hugging Face": ("hugging face", "huggingface", "transformers library"),
    "LLMs": ("llm", "llms", "large language models"),
    "LangChain": ("langchain",),
    "RAG": ("=RAG", "retrieval-augmented generation"),
    "NLP": ("nlp", "natural language processing"),
    "Computer Vision": ("computer vision", "opencv"),
    "MLOps": ("mlops",),
    "Spark": ("apache spark", "pyspark", "!=Spark"),
    "Airflow": ("airflow",),
    "dbt": ("=dbt",),
    "Databricks": ("databricks",),
    "Hadoop": ("hadoop",),
    "Flink": ("flink",),
    "ETL": ("=ETL", "elt"),
    "Tableau": ("tableau",),
    "Power BI": ("power bi", "powerbi"),
    # Testing & quality
    "Pytest": ("pytest",),
    "Jest": ("jest",),
    "Vitest": ("vitest",),
    "Cypress": ("cypress",),
    "Playwright": ("playwright",),
    "Selenium": ("selenium",),
    "JUnit": ("junit",),
    "TDD": ("=TDD", "test-driven development"),
    # Practices & security
    "Agile": ("agile", "scrum", "kanban"),
    "System Design": ("system design", "distributed systems"),
    "OAuth": ("oauth", "oauth2", "openid connect", "oidc"),
    "Web Security": ("owasp",),
    "Blockchain": ("blockchain", "web3", "ethereum"),
}

FRONTEND = {"React", "Next.js", "Vue.js", "Nuxt", "Angular", "Svelte", "Redux", "HTML", "CSS", "Sass",
            "Tailwind CSS", "Webpack", "Vite", "jQuery", "TypeScript", "JavaScript"}
BACKEND = {"Node.js", "Express", "NestJS", "Django", "Flask", "FastAPI", "Spring Boot", "Ruby on Rails",
           "Laravel", ".NET", "Go", "Java", "Python", "PHP", "Ruby", "Elixir", "PostgreSQL", "MySQL",
           "MongoDB", "Redis", "Kafka", "RabbitMQ", "gRPC", "REST APIs", "Microservices", "GraphQL"}
MOBILE = {"Swift", "SwiftUI", "Kotlin", "React Native", "Flutter", "Dart", "Objective-C", "Jetpack Compose"}
DEVOPS = {"Kubernetes", "Terraform", "Docker", "Helm", "Ansible", "Pulumi", "CI/CD", "Jenkins", "ArgoCD",
          "Prometheus", "Grafana", "AWS", "GCP", "Azure", "Linux"}
DATA = {"Spark", "Airflow", "dbt", "Databricks", "Snowflake", "BigQuery", "Redshift", "Hadoop", "Flink",
        "ETL", "Kafka", "SQL", "Pandas"}
ML = {"PyTorch", "TensorFlow", "Keras", "scikit-learn", "Hugging Face", "LLMs", "LangChain", "RAG", "NLP",
      "Computer Vision", "MLOps"}


@dataclass(frozen=True)
class SkillPattern:
    canonical: str
    regex: re.Pattern[str]
    skills_section_only: bool


def _compile(canonical: str, alias: str) -> SkillPattern:
    section_only = alias.startswith("!")
    alias = alias.lstrip("!")
    case_sensitive = alias.startswith("=")
    alias = alias.lstrip("=")
    # Custom boundaries so "C" doesn't match inside "C++"/"C#"/"Objective-C" and "js" doesn't match inside
    # "node.js", while "HTML/CSS" and "Python-based" still match.
    body = r"\s+".join(re.escape(part) for part in alias.split())
    pattern = rf"(?<![\w+#.\-]){body}(?![\w+#]|\.\w)"
    flags = 0 if case_sensitive else re.IGNORECASE
    return SkillPattern(canonical, re.compile(pattern, flags), section_only)


SKILL_PATTERNS: tuple[SkillPattern, ...] = tuple(
    _compile(canonical, alias) for canonical, aliases in SKILLS.items() for alias in aliases
)

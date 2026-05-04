import argparse
import os
import re
import json
import hashlib
import shutil
import time
import concurrent.futures
from collections import defaultdict
from dataclasses import dataclass

from tree_sitter import Language, Parser
from git import Repo
import requests
import networkx as nx
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg') 


BASE_OUTPUT_DIR = "kmp_analysis"
os.makedirs(BASE_OUTPUT_DIR, exist_ok=True)

GITHUB_TOKEN = None
HEADERS = {}


def configure_github_token(token=None):
    """Configure GitHub token/headers from CLI token or environment variable."""
    global GITHUB_TOKEN, HEADERS
    candidate = token.strip() if token else os.environ.get("GITHUB_TOKEN")
    GITHUB_TOKEN = candidate if candidate else None
    HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}


configure_github_token()


# Only mine repositories with licenses that generally allow reuse/analysis.
ALLOWED_LICENSE_SPDX = {
    "MIT",
    "APACHE-2.0",
    "BSD-2-CLAUSE",
    "BSD-3-CLAUSE",
    "ISC",
    "MPL-2.0",
    "EPL-2.0",
    "LGPL-2.1-ONLY",
    "LGPL-2.1-OR-LATER",
    "LGPL-3.0-ONLY",
    "LGPL-3.0-OR-LATER",
    "GPL-2.0-ONLY",
    "GPL-2.0-OR-LATER",
    "GPL-3.0-ONLY",
    "GPL-3.0-OR-LATER",
    "AGPL-3.0-ONLY",
    "AGPL-3.0-OR-LATER",
    "UNLICENSE",
    "CC0-1.0",
    "CDDL-1.0",
    "CDDL-1.1",
    "BSL-1.0",
    "ZLIB",
    "ARTISTIC-2.0",
}

LICENSE_ALIASES = {
    "GPL-2.0": "GPL-2.0-ONLY",
    "GPL-3.0": "GPL-3.0-ONLY",
    "LGPL-2.1": "LGPL-2.1-ONLY",
    "LGPL-3.0": "LGPL-3.0-ONLY",
    "AGPL-3.0": "AGPL-3.0-ONLY",
}


def normalize_spdx_id(spdx_id):
    if not spdx_id:
        return ""
    normalized = spdx_id.strip().upper()
    return LICENSE_ALIASES.get(normalized, normalized)


def is_license_allowed(spdx_id):
    normalized = normalize_spdx_id(spdx_id)
    if not normalized:
        return False, "missing"
    if normalized in {"NOASSERTION", "NONE"}:
        return False, normalized
    if normalized in ALLOWED_LICENSE_SPDX:
        return True, normalized
    return False, normalized


def fetch_repository_license(owner, repo):
    """Fetch SPDX license id for a repository from GitHub REST API."""
    url = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return None
        license_info = resp.json().get("license") or {}
        return license_info.get("spdx_id")
    except Exception:
        return None


# ==========================================================
# REPOSITORY MANAGEMENT
# ==========================================================

NON_PRODUCTION_PATH_MARKERS = (
    "/src/test/",
    "/src/androidtest/",
    "/src/commontest/",
    "/src/jvmtest/",
    "/src/iostest/",
    "/src/jstest/",
    "/template/",
    "/templates/",
    "/archetype/",
    "/boilerplate/",
    "/starter/",
    "/benchmark/",
    "/benchmarks/",
    "/experimental/",
    "/build/",
    "/generated/",
)

NON_PRODUCTION_SEGMENTS = {
    "sample",
    "samples",
    "example",
    "examples",
    "demo",
    "demos",
}

PACKAGE_DOMAIN_SEGMENTS = {
    "com",
    "org",
    "io",
    "net",
    "dev",
    "app",
    "me",
    "co",
}

NON_APP_INFRA_PATH_MARKERS = (
    "/buildsrc/",
    "/build-logic/",
    "/gradle-plugins/",
    "/convention-plugins/",
    "/tooling/",
    "/scripts/",
)


def normalize_path(path):
    return path.replace("\\", "/")


def is_non_production_kotlin_path(file_path):
    path_l = normalize_path(file_path).lower()
    if any(marker in path_l for marker in NON_PRODUCTION_PATH_MARKERS):
        return True

    path_parts = [part for part in path_l.split("/") if part]
    for index, segment in enumerate(path_parts):
        if segment not in NON_PRODUCTION_SEGMENTS:
            continue

        previous_segment = path_parts[index - 1] if index > 0 else ""
        if previous_segment in PACKAGE_DOMAIN_SEGMENTS:
            continue
        return True

    return False


def is_non_app_infrastructure_path(file_path):
    path_l = normalize_path(file_path).lower()
    return any(marker in path_l for marker in NON_APP_INFRA_PATH_MARKERS)


def is_app_module_path(path):
    path_l = normalize_path(path).lower()
    return any(marker in path_l for marker in ["/app/", "/androidapp/", "/composeapp/", "/application/"])

def clone_repository(owner, repo, repo_dir):
    """Clone a repository from GitHub with shallow depth."""
    clone_path = os.path.join(repo_dir, "repo")

    if os.path.exists(clone_path):
        return clone_path

    try:
        Repo.clone_from(
            f"https://github.com/{owner}/{repo}.git",
            clone_path,
            depth=1
        )
        return clone_path
    except Exception as e:
        print(f"Clone failed: {owner}/{repo} - {e}")
        return None


def list_kotlin_files(repo_path, include_non_production=False):
    files = []
    for root, _, filenames in os.walk(repo_path):
        for file in filenames:
            if file.endswith(".kt"):
                full_path = os.path.join(root, file)
                if not include_non_production and (
                    is_non_production_kotlin_path(full_path)
                    or is_non_app_infrastructure_path(full_path)
                ):
                    continue
                files.append(full_path)
    return files


def load_repo_files(repo_path, include_non_production=False):
    repo_files = {}
    kotlin_files = list_kotlin_files(repo_path, include_non_production=include_non_production)

    for file in kotlin_files:
        try:
            with open(file, "r", encoding="utf-8") as f:
                repo_files[file] = f.read()
        except Exception as e:
            pass

    return repo_files


def repository_looks_like_application(repo_path, repo_files=None):
    if repo_files is None:
        repo_files = load_repo_files(repo_path, include_non_production=False)

    if not repo_files:
        return False, "no production Kotlin files"

    production_files = {
        path: content
        for path, content in repo_files.items()
        if not is_non_production_kotlin_path(path)
        and not is_non_app_infrastructure_path(path)
    }

    if not production_files:
        return False, "no production Kotlin files"

    for build_file in [
        "app/build.gradle",
        "app/build.gradle.kts",
        "androidApp/build.gradle",
        "androidApp/build.gradle.kts",
        "composeApp/build.gradle",
        "composeApp/build.gradle.kts",
        "application/build.gradle",
        "application/build.gradle.kts",
    ]:
        build_path = os.path.join(repo_path, build_file)
        if not os.path.exists(build_path):
            continue
        try:
            build_content = read_file(build_path).lower()
        except Exception:
            continue
        if "com.android.application" in build_content:
            return True, f"found app plugin in {build_file}"

    kotlin_paths = [normalize_path(path) for path in production_files.keys()]
    file_names = {os.path.basename(path) for path in kotlin_paths}

    if "MainActivity.kt" in file_names:
        return True, "found MainActivity.kt"

    if "MainViewController.kt" in file_names:
        return True, "found MainViewController.kt"
    if any("/androidapp/src/main/" in path.lower() for path in kotlin_paths) and "MainAndroidApp.kt" in file_names:
        return True, "found MainAndroidApp.kt"

    for path, content in production_files.items():
        content_l = content.lower()
        path_l = normalize_path(path).lower()
        if "componentactivity" in content_l and "setcontent" in content_l:
            if is_app_module_path(path_l) or os.path.basename(path_l) == "mainactivity.kt":
                return True, "found ComponentActivity with setContent"
        if "@composable" in content and re.search(r"\bfun\s+App\s*\(", content):
            if is_app_module_path(path_l) or path_l.endswith("/app.kt"):
                return True, "found @Composable App entrypoint"
        if re.search(r"\bfun\s+main\s*\(", content):
            if is_app_module_path(path_l):
                return True, "found main() entrypoint in app module"

            file_name = os.path.basename(path_l)
            has_ui_bootstrap = any(
                marker in content_l
                for marker in [
                    "application(",
                    "singlewindowapplication",
                    "window(",
                    "componentactivity",
                    "setcontent",
                    "@composable",
                    "uiapplicationmain",
                ]
            )
            if file_name in {"main.kt", "app.kt"} and has_ui_bootstrap:
                return True, "found UI main() entrypoint"

    return False, "no app entrypoint indicators"


def _init_parser():
    language_path = './tree-sitter-kotlin.so'
    grammar_path = './tree-sitter-kotlin'
    if not os.path.exists(language_path) and os.path.exists(grammar_path):
        Language.build_library(language_path, [grammar_path])
    kotlin_language = Language(language_path, 'kotlin')
    kotlin_parser = Parser()
    kotlin_parser.set_language(kotlin_language)
    return kotlin_parser


parser = _init_parser()


ROLE_HEURISTICS = {
    "Test": {
        "annotations": ["@Test", "@RunWith", "@HiltAndroidTest"],
        "imports": ["org.junit", "androidx.test", "mockk", "kotlin.test"],
        "inheritance": [],
        "name_contains": ["test", "benchmark"],
    },
    "View": {
        "annotations": ["@Composable", "@Preview", "@AndroidEntryPoint"],
        "imports": ["androidx.compose", "androidx.navigation", "ComponentActivity"],
        "inheritance": ["ComponentActivity", "Fragment", "Screen", "Activity"],
        "name_contains": ["view", "screen", "activity", "fragment"],
    },
    "ViewModel": {
        "annotations": ["@HiltViewModel", "@Inject"],
        "imports": ["androidx.lifecycle.ViewModel", "kotlinx.coroutines.flow"],
        "inheritance": ["ViewModel"],
        "name_contains": ["viewmodel", "vm"],
    },
    "Presenter": {
        "annotations": [],
        "imports": [],
        "inheritance": ["Presenter"],
        "name_contains": ["presenter"],
    },
    "Controller": {
        "annotations": [],
        "imports": ["ktor.server", "springframework.web", "Controller"],
        "inheritance": ["Controller"],
        "name_contains": ["controller"],
    },
    "Interactor": {
        "annotations": [],
        "imports": [],
        "inheritance": ["Interactor"],
        "name_contains": ["interactor"],
    },
    "Repository": {
        "annotations": ["@Singleton", "@Inject"],
        "imports": ["androidx.room", "kotlinx.coroutines", "sqldelight"],
        "inheritance": ["Repository"],
        "name_contains": ["repository", "repo", "datasource", "dao", "database", "localdata"],
    },
    "Service": {
        "annotations": ["@Singleton", "@Inject"],
        "imports": ["retrofit2", "okhttp3", "io.ktor.client", "kotlinx.coroutines"],
        "inheritance": ["Service"],
        "name_contains": ["service", "api", "client", "remote"],
    },
    "UseCase": {
        "annotations": [],
        "imports": [],
        "inheritance": ["UseCase"],
        "name_contains": ["usecase", "interactor"],
    },
    "Model": {
        "annotations": ["@Serializable"],
        "imports": [],
        "inheritance": ["Model"],
        "name_contains": ["model", "state"],
    },
    "Entity": {
        "annotations": ["@Serializable", "@Parcelize", "@Entity"],
        "imports": ["kotlinx.serialization", "androidx.room"],
        "inheritance": [],
        "name_contains": ["entity", "dto", "payload"],
    },
    "Intent": {
        "annotations": [],
        "imports": [],
        "inheritance": ["Intent"],
        "name_contains": [
            "intent", "intents", "action", "actions", "event", "events",
            "wish", "wishes", "reducer", "reducers", "middleware", "middlewares",
            "sideeffect", "sideeffects",
        ],
    },
    "Router": {
        "annotations": [],
        "imports": ["androidx.navigation.NavController", "androidx.navigation.NavHost"],
        "inheritance": ["Router"],
        "name_contains": ["router", "navigator", "navigation"],
    },
    "DI": {
        "annotations": ["@Module", "@Provides", "@InstallIn", "@Binds"],
        "imports": ["dagger", "koin"],
        "inheritance": [],
        "name_contains": ["module", "inject", "dependency"],
    },
    "State": {
        "annotations": [],
        "imports": [],
        "inheritance": ["State"],
        "name_contains": ["state", "states"],
    },
}


ROLE_RESOLUTION_ORDER = [
    "Test",
    "ViewModel",
    "Presenter",
    "Controller",
    "Interactor",
    "UseCase",
    "Repository",
    "Service",
    "Intent",
    "State",
    "Router",
    "Entity",
    "Model",
    "View",
    "DI",
]

ARCHITECTURE_RELEVANT_ROLES = {
    "View",
    "ViewModel",
    "Presenter",
    "Controller",
    "Interactor",
    "UseCase",
    "Model",
    "Entity",
    "Intent",
    "State",
    "Router",
}

ABSTRACT_ROLE_MAP = {
    "View": "UI",
    "ViewModel": "PresentationLogic",
    "Presenter": "PresentationLogic",
    "Controller": "PresentationLogic",
    "Model": "DomainData",
    "Entity": "DomainData",
    "State": "DomainData",
    "Interactor": "Interaction",
    "UseCase": "Interaction",
    "Intent": "Event",
    "Router": "Navigation",
}

IGNORED_ARCHITECTURE_NAME_TOKENS = {
    "repository",
    "repo",
    "service",
    "dto",
    "api",
    "client",
    "manager",
    "utility",
    "util",
    "configuration",
    "config",
    "logger",
}

GRAPH_VISUAL_MAX_NODES = 50
GRAPH_MAX_OUT_EDGES_PER_NODE = 4
GRAPH_VISUAL_MAX_OUT_EDGES_PER_NODE = 3

ARCHITECTURE_ROLE_ORDER = [
    "View",
    "ViewModel",
    "Presenter",
    "Controller",
    "Interactor",
    "UseCase",
    "Intent",
    "Router",
    "Model",
    "Entity",
]

ARCHITECTURE_ALLOWED_ROLE_EDGES = {
    "View": {"ViewModel", "Presenter", "Controller", "Intent", "State", "Router"},
    "ViewModel": {"Model", "Entity", "UseCase", "Intent", "State"},
    "Presenter": {"View", "Interactor", "Model", "Entity", "Router", "State"},
    "Controller": {"View", "Model", "Entity", "UseCase", "State"},
    "Interactor": {"Model", "Entity", "UseCase", "Router", "State"},
    "UseCase": {"Model", "Entity", "State"},
    "Intent": {"ViewModel", "Interactor", "UseCase", "Model", "Entity", "State"},
    "State": {"View", "ViewModel", "Presenter", "Controller"},
    "Router": {"View", "Presenter", "Controller"},
    "Model": {"Entity", "View"},
    "Entity": set(),
}


URI_ROLE_FOLDER_TOKENS = {
    "Test": {"test", "tests", "androidtest", "benchmark", "benchmarks"},
    "View": {
        "view", "views", "ui", "screen", "screens", "activity", "activities", "fragment", "fragments",
        "compose", "presentation", "component", "components", "widget", "widgets", "layout", "layouts",
        "dialog", "dialogs", "sheet", "sheets", "adapter", "adapters", "holder", "holders", "theme", "themes"
    },
    "ViewModel": {"viewmodel", "viewmodels", "screenmodel", "screenmodels", "vm"},
    "Presenter": {"presenter", "presenters"},
    "Controller": {"controller", "controllers"},
    "Interactor": {"interactor", "interactors"},
    "Repository": {"repository", "repositories", "repo", "repos", "datasource", "datasources", "dao", "daos", "database", "db", "cache"},
    "Service": {"service", "services", "api", "network", "remote", "client", "clients"},
    "UseCase": {"usecase", "usecases", "use_case", "use_cases"},
    "Model": {"model", "models", "state", "states"},
    "Entity": {"entity", "entities", "dto", "dtos", "payload", "payloads"},
    "Intent": {"intent", "intents", "action", "actions", "event", "events"},
    "Router": {"router", "routers", "navigation", "navigator", "nav"},
    "DI": {"di", "module", "modules", "inject", "injection", "dependency", "dependencies"},
}


URI_ROLE_STRONG_PAIRS = {
    "ViewModel": [("presentation", "viewmodel"), ("ui", "viewmodel"), ("feature", "viewmodel"), ("ui", "model")],
    "Presenter": [("presentation", "presenter"), ("ui", "presenter")],
    "Repository": [("data", "repository"), ("data", "datasource"), ("data", "dao")],
    "Service": [("data", "remote"), ("network", "service")],
    "UseCase": [("domain", "usecase"), ("domain", "interactor")],
    "Entity": [("data", "entity"), ("data", "dto")],
    "Router": [("navigation", "router")],
    "DI": [("di", "module")],
}


URI_COMMON_WORD_STOPWORDS = {
    "src", "main", "java", "kotlin", "com", "org", "io", "app", "apps", "core", "common",
    "shared", "internal", "impl", "base", "android",
    "desktop", "ios", "jvm", "js", "build", "release", "debug"
}


URI_ROLE_COMMON_WORDS = {
    role: set(config.get("name_contains", []))
    for role, config in ROLE_HEURISTICS.items()
}
URI_ROLE_COMMON_WORDS["View"].update({"ui", "screen", "activity", "fragment", "compose"})
URI_ROLE_COMMON_WORDS["View"].update({"component", "widget", "layout", "dialog", "sheet", "adapter", "holder", "theme", "composable"})
URI_ROLE_COMMON_WORDS["ViewModel"].update({"viewmodel", "screenmodel", "vm"})
URI_ROLE_COMMON_WORDS["UseCase"].update({"usecase", "usecases"})
URI_ROLE_COMMON_WORDS["DI"].update({"di", "module", "provide", "inject", "injection"})


COMPOSITE_PATH_TOKENS = {
    "viewmodel",
    "viewholder",
    "itemdecoration",
    "screenmodel",
    "uistate",
    "viewstate",
    "testrunner",
    "usecase",
    "datasource",
    "repository",
    "presenter",
    "interactor",
    "controller",
    "router",
}


COMPOSABLE_VIEW_SUFFIXES = (
    "screen",
    "view",
    "page",
    "dialog",
    "sheet",
    "component",
)


@dataclass
class ClassInfo:
    imports: list
    annotations: list
    inheritances: list
    symbols: set
    package_name: str
    is_data_class: bool


def read_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def extract_nodes(node, source_bytes, kind=None):
    results = []
    if kind is None or node.type == kind:
        text = source_bytes[node.start_byte:node.end_byte].decode(errors='ignore')
        results.append((node, text))
    for child in node.children:
        results.extend(extract_nodes(child, source_bytes, kind))
    return results


def _extract_class_name(class_node, source_bytes):
    for child in class_node.children:
        if child.type in ("type_identifier", "identifier"):
            return source_bytes[child.start_byte:child.end_byte].decode(errors='ignore')
    class_text = source_bytes[class_node.start_byte:class_node.end_byte].decode(errors='ignore')
    match = re.search(r"\bclass\s+([A-Z][A-Za-z0-9_]*)", class_text)
    return match.group(1) if match else None


def _split_words(value):
    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    chunks = re.split(r"[^A-Za-z0-9]+", camel_split)
    tokens = [chunk.lower() for chunk in chunks if chunk]

    compact = re.sub(r"[^a-z0-9]", "", value.lower())
    if compact:
        for composite in COMPOSITE_PATH_TOKENS:
            if composite in compact:
                tokens.append(composite)

    return tokens


def _token_matches_name(token, name_lower, name_tokens):
    if token in name_tokens:
        return True
    if len(token) <= 2:
        return False
    return token in name_lower


def _is_confident_uri_role(uri_role, uri_method, score_map):
    if uri_role == "Other" or not score_map:
        return False

    role_evidence = score_map.get(uri_role, set())
    if not role_evidence:
        return False

    if uri_method == "uri_folder":
        return True

    if uri_method == "common_words":
        return len(score_map) == 1

    return False


def _role_order(role_name):
    if role_name in ARCHITECTURE_ROLE_ORDER:
        return ARCHITECTURE_ROLE_ORDER.index(role_name)
    return len(ARCHITECTURE_ROLE_ORDER)


def _is_architecture_edge_allowed(src_role, dst_role):
    if src_role not in ARCHITECTURE_RELEVANT_ROLES:
        return False
    if dst_role not in ARCHITECTURE_RELEVANT_ROLES:
        return False
    return dst_role in ARCHITECTURE_ALLOWED_ROLE_EDGES.get(src_role, set())


def _is_non_architecture_support_class(class_name):
    class_tokens = set(_split_words(class_name))
    if not class_tokens:
        return False

    has_core_architecture_token = bool(
        class_tokens.intersection({
            "view",
            "screen",
            "activity",
            "fragment",
            "composable",
            "viewmodel",
            "presenter",
            "controller",
            "interactor",
            "usecase",
            "intent",
            "action",
            "event",
            "router",
            "navigator",
            "navigation",
            "model",
            "entity",
            "state",
        })
    )
    if has_core_architecture_token:
        return False

    return bool(class_tokens.intersection(IGNORED_ARCHITECTURE_NAME_TOKENS))


def _class_is_architecture_relevant(class_name, role):
    if role not in ARCHITECTURE_RELEVANT_ROLES:
        return False
    return not _is_non_architecture_support_class(class_name)


def _is_protocol_container_name(class_name):
    return class_name == "BaseContract" or class_name.endswith("Contract") or class_name in {
        "View",
        "Presenter",
        "Interactor",
        "Router",
    }


def _is_protocol_container_class(class_name, info, file_path=None):
    class_name_l = class_name.lower()
    package_l = info.package_name.lower()
    path_l = (file_path or "").replace("\\", "/").lower()

    if class_name == "BaseContract" or class_name_l.endswith("contract"):
        return True

    if class_name in {"View", "Presenter", "Interactor", "Router"}:
        return any(
            marker in package_l or marker in path_l
            for marker in [".base", ".contract", "/base/", "/contract/"]
        )

    return False


def extract_compose_view_functions(source_code):
    functions = []
    pattern = re.compile(
        r"((?:@[A-Za-z_][A-Za-z0-9_]*(?:\([^)]*\))?\s*)+)fun\s+([A-Z][A-Za-z0-9_]*)\s*\(",
        re.MULTILINE,
    )

    for annotations, function_name in pattern.findall(source_code):
        function_name_l = function_name.lower()
        if "@Composable" not in annotations:
            continue
        if function_name == "App" or function_name_l.endswith(COMPOSABLE_VIEW_SUFFIXES):
            functions.append(function_name)

    return functions


def collect_project_metadata(project_dir):
    repo_files = load_repo_files(project_dir, include_non_production=False)
    has_app_entrypoint, entrypoint_reason = repository_looks_like_application(project_dir, repo_files=repo_files)

    compose_views = set()
    for file_path, source in repo_files.items():
        if is_non_production_kotlin_path(file_path) or is_non_app_infrastructure_path(file_path):
            continue
        compose_views.update(extract_compose_view_functions(source))

    return {
        "compose_views": sorted(compose_views),
        "has_app_entrypoint": has_app_entrypoint,
        "entrypoint_reason": entrypoint_reason,
    }


def _repo_relative_uri(file_path, repo_root=None):
    if not repo_root:
        return file_path.replace("\\", "/")
    try:
        return os.path.relpath(file_path, repo_root).replace("\\", "/")
    except ValueError:
        return file_path.replace("\\", "/")


def score_file_role_from_uri(file_path, repo_root=None):
    """Infer file role by URI conventions using deterministic evidence."""
    relative_uri = _repo_relative_uri(file_path, repo_root=repo_root)
    parts = [p for p in relative_uri.split("/") if p]

    if not parts:
        return "Other", "unclassified", {}

    dir_parts = parts[:-1]
    file_stem = os.path.splitext(parts[-1])[0]

    dir_tokens = []
    for part in dir_parts:
        dir_tokens.extend(_split_words(part))
    file_tokens = _split_words(file_stem)

    dir_token_set = {
        token for token in dir_tokens
        if token and token not in URI_COMMON_WORD_STOPWORDS
    }
    file_token_set = {
        token for token in file_tokens
        if token and token not in URI_COMMON_WORD_STOPWORDS
    }

    evidence = defaultdict(set)
    recognized_folder = False

    for role, token_pairs in URI_ROLE_STRONG_PAIRS.items():
        for token_a, token_b in token_pairs:
            if token_a in dir_token_set and token_b in dir_token_set:
                evidence[role].add("strong_pair")
                recognized_folder = True

    for role, folder_tokens in URI_ROLE_FOLDER_TOKENS.items():
        matched_dir_tokens = dir_token_set.intersection(folder_tokens)
        matched_file_tokens = file_token_set.intersection(folder_tokens)

        if matched_dir_tokens:
            evidence[role].add("folder_token")
            recognized_folder = True
        if matched_file_tokens:
            evidence[role].add("file_token")

    if recognized_folder:
        ordered = sorted(evidence.keys(), key=lambda role: ROLE_RESOLUTION_ORDER.index(role) if role in ROLE_RESOLUTION_ORDER else len(ROLE_RESOLUTION_ORDER))
        if ordered:
            return ordered[0], "uri_folder", {role: set(signals) for role, signals in evidence.items()}
        return "Other", "unclassified", {}

    all_tokens = dir_token_set | file_token_set
    for role, common_words in URI_ROLE_COMMON_WORDS.items():
        matches = all_tokens.intersection(common_words)
        if matches:
            evidence[role].add("common_word")

    ordered = sorted(evidence.keys(), key=lambda role: ROLE_RESOLUTION_ORDER.index(role) if role in ROLE_RESOLUTION_ORDER else len(ROLE_RESOLUTION_ORDER))
    if ordered:
        return ordered[0], "common_words", {role: set(signals) for role, signals in evidence.items()}
    return "Other", "unclassified", {}


def parse_kotlin_file(source_code):
    source_bytes = source_code.encode("utf8")
    tree = parser.parse(source_bytes)
    root = tree.root_node

    imports_ast = [text.strip() for _, text in extract_nodes(root, source_bytes, 'import_directive')]
    if not imports_ast:
        imports_ast = [text.strip() for _, text in extract_nodes(root, source_bytes, 'import_header')]

    imports_regex = [
        f"import {match.group(1).strip()}"
        for match in re.finditer(r"^\s*import\s+([^\s;]+)", source_code, re.MULTILINE)
    ]

    imports = []
    seen_imports = set()
    for item in imports_ast + imports_regex:
        cleaned = item.strip()
        if cleaned and cleaned not in seen_imports:
            seen_imports.add(cleaned)
            imports.append(cleaned)

    package_match = re.search(r"^\s*package\s+([a-zA-Z0-9_.]+)", source_code, re.MULTILINE)
    package_name = package_match.group(1) if package_match else ""

    classes = {}
    class_nodes = extract_nodes(root, source_bytes, 'class_declaration')
    for class_node, class_text in class_nodes:
        class_name = _extract_class_name(class_node, source_bytes)
        if not class_name:
            continue

        header_text = class_text.split("{", 1)[0]
        class_annotations = re.findall(r"@[A-Za-z_][A-Za-z0-9_]*", header_text)

        super_nodes = [n for n in class_node.children if n.type == 'super_type_list']
        inheritances = []
        for super_node in super_nodes:
            super_text = source_bytes[super_node.start_byte:super_node.end_byte].decode(errors='ignore')
            inheritances.extend(re.findall(r"\b([A-Z][A-Za-z0-9_]*)\b", super_text))

        symbols = set(re.findall(r"\b([A-Z][A-Za-z0-9_]*)\b", class_text))
        symbols.discard(class_name)

        classes[class_name] = ClassInfo(
            imports=imports,
            annotations=class_annotations,
            inheritances=inheritances,
            symbols=symbols,
            package_name=package_name,
            is_data_class=("data class" in class_text),
        )

    return classes


def _is_test_context(class_name, info, file_path=None):
    class_tokens = set(_split_words(class_name))
    package_l = info.package_name.lower()
    path_l = (file_path or "").replace("\\", "/").lower()

    has_test_path = bool(re.search(r"/src/[^/]*test/", path_l)) or "/benchmark/" in path_l
    has_test_package = ".test" in package_l or ".benchmark" in package_l
    has_test_name = any(token in class_tokens for token in {"test", "benchmark", "testrunner"})
    has_test_import = any(
        any(key in imp.lower() for key in ["org.junit", "androidx.test", "kotlin.test", "robolectric", "mockk"])
        for imp in info.imports
    )

    return has_test_path or has_test_package or has_test_name or has_test_import


def score_role(class_name, info, file_path=None):
    class_name_l = class_name.lower()
    class_name_tokens = set(_split_words(class_name))

    if _is_protocol_container_class(class_name, info, file_path=file_path):
        return "Other", {"Other": ["protocol_container"]}

    if _is_test_context(class_name, info, file_path=file_path):
        return "Other", {"Other": ["test_context"]}

    if _is_non_architecture_support_class(class_name):
        return "Other", {"Other": ["support_class"]}

    evidence = defaultdict(set)

    package_tokens = set(_split_words(info.package_name))
    annotation_names = [ann.lstrip("@").lower() for ann in info.annotations]
    imports_l = [imp.lower() for imp in info.imports]
    inheritances_l = [inh.lower() for inh in info.inheritances]

    for role, hints in ROLE_HEURISTICS.items():
        for token in hints["name_contains"]:
            if _token_matches_name(token, class_name_l, class_name_tokens):
                evidence[role].add("name")

        for ann_marker in hints["annotations"]:
            marker_l = ann_marker.lstrip("@").lower()
            if any(marker_l in ann for ann in annotation_names):
                evidence[role].add("annotation")

        for import_marker in hints["imports"]:
            marker_l = import_marker.lower()
            if any(marker_l in imp for imp in imports_l):
                evidence[role].add("import")

        for parent_marker in hints["inheritance"]:
            marker_l = parent_marker.lower()
            if any(marker_l in inh for inh in inheritances_l):
                evidence[role].add("inheritance")

    for role, folder_tokens in URI_ROLE_FOLDER_TOKENS.items():
        for token in package_tokens.intersection(folder_tokens):
            evidence[role].add("package")

    if info.is_data_class:
        evidence["Model"].add("data_class")
        if any(token in class_name_tokens for token in {"entity", "payload"}):
            evidence["Entity"].add("data_class")

    if not evidence:
        return "Other", {}

    ignored_roles = {"Repository", "Service", "DI", "Test"}

    if class_name_l.endswith((
        "intent", "intents", "action", "actions", "event", "events",
        "wish", "wishes", "reducer", "reducers", "middleware", "middlewares",
        "sideeffect", "sideeffects",
    )):
        signals = evidence.get("Intent", {"name"})
        return "Intent", {"Intent": sorted(signals)}

    if class_name_l.endswith(("viewstate", "viewstates", "screenstate", "screenstates", "state", "states")):
        signals = evidence.get("State", {"name"})
        return "State", {"State": sorted(signals)}

    def finalize(role_name, signals):
        if role_name in ignored_roles:
            return "Other", {"Other": ["ignored_role"]}
        return role_name, {role_name: sorted(signals)}

    for role in ROLE_RESOLUTION_ORDER:
        signals = evidence.get(role, set())
        if "annotation" in signals or "inheritance" in signals:
            return finalize(role, signals)

    for role in ROLE_RESOLUTION_ORDER:
        signals = evidence.get(role, set())
        if "name" in signals and ("package" in signals or "import" in signals):
            return finalize(role, signals)

    for role in ROLE_RESOLUTION_ORDER:
        signals = evidence.get(role, set())
        if "name" in signals:
            return finalize(role, signals)

    for role in ROLE_RESOLUTION_ORDER:
        signals = evidence.get(role, set())
        if signals:
            return finalize(role, signals)

    return "Other", {}


def build_graph(project_dir):
    class_info = {}
    class_source_paths = {}
    duplicate_class_names = set()

    for root, dirs, files in os.walk(project_dir):
        dirs.sort()
        files.sort()
        for file_name in files:
            if not file_name.endswith('.kt'):
                continue
            path = os.path.join(root, file_name)
            if is_non_production_kotlin_path(path) or is_non_app_infrastructure_path(path):
                continue
            source = read_file(path)
            parsed_classes = parse_kotlin_file(source)
            for class_name, info in parsed_classes.items():
                if class_name in class_info:
                    duplicate_class_names.add(class_name)
                    continue
                class_source_paths[class_name] = path
                class_info[class_name] = info

    class_roles = {}
    for class_name, info in class_info.items():
        role, _ = score_role(class_name, info, file_path=class_source_paths.get(class_name))
        class_roles[class_name] = role

    relevant_classes = {
        cls for cls, role in class_roles.items()
        if _class_is_architecture_relevant(cls, role)
    }

    graph = defaultdict(set)
    for class_name in relevant_classes:
        graph[class_name]

    for class_name, info in class_info.items():
        if class_name not in relevant_classes:
            continue

        src_role = class_roles.get(class_name, "Other")
        for parent in info.inheritances:
            if parent in duplicate_class_names:
                continue
            if parent in relevant_classes:
                dst_role = class_roles.get(parent, "Other")
                if _is_architecture_edge_allowed(src_role, dst_role):
                    graph[class_name].add(parent)

        for imp in info.imports:
            imported_path = imp.replace("import", "").strip().split(" as ")[0]
            target = imported_path.split('.')[-1]
            if target in duplicate_class_names:
                continue
            if target in relevant_classes and target != class_name:
                dst_role = class_roles.get(target, "Other")
                if _is_architecture_edge_allowed(src_role, dst_role):
                    graph[class_name].add(target)

        for symbol in info.symbols:
            if symbol in duplicate_class_names:
                continue
            if symbol in relevant_classes and symbol != class_name:
                dst_role = class_roles.get(symbol, "Other")
                if _is_architecture_edge_allowed(src_role, dst_role):
                    graph[class_name].add(symbol)

    class_roles = refine_roles_with_graph(class_roles, graph)
    focused_graph = focus_dependency_graph(graph, class_roles)
    class_roles = refine_roles_with_graph(class_roles, focused_graph)
    focused_graph = focus_dependency_graph(graph, class_roles)
    return class_roles, focused_graph


def focus_dependency_graph(graph, class_roles):
    focused = defaultdict(set)

    relevant_nodes = {
        cls for cls, role in class_roles.items()
        if _class_is_architecture_relevant(cls, role)
    }

    for node in relevant_nodes:
        focused[node]

    for src, targets in graph.items():
        if src not in relevant_nodes:
            continue
        src_role = class_roles.get(src, "Other")
        ordered_targets = sorted(
            [dst for dst in targets if dst in relevant_nodes],
            key=lambda dst: (_role_order(class_roles.get(dst, "Other")), dst),
        )

        selected = []
        for dst in ordered_targets:
            dst_role = class_roles.get(dst, "Other")
            if _is_architecture_edge_allowed(src_role, dst_role):
                selected.append(dst)
            if len(selected) >= GRAPH_MAX_OUT_EDGES_PER_NODE:
                break

        focused[src].update(selected)

    connected_nodes = set()
    for src, targets in focused.items():
        if targets:
            connected_nodes.add(src)
        for dst in targets:
            connected_nodes.add(dst)

    if not connected_nodes:
        return focused

    trimmed = defaultdict(set)
    for node in connected_nodes:
        trimmed[node] = {dst for dst in focused.get(node, set()) if dst in connected_nodes}

    return trimmed


def refine_roles_with_graph(class_roles, graph, iterations=1):
    reverse_graph = defaultdict(set)
    for src, targets in graph.items():
        for dst in targets:
            reverse_graph[dst].add(src)

    for _ in range(iterations):
        updated = dict(class_roles)
        for cls in class_roles:
            if _is_protocol_container_name(cls):
                continue

            if class_roles.get(cls) != "Other":
                continue

            neighbor_roles = []
            for out_node in graph.get(cls, set()):
                role = class_roles.get(out_node, "Other")
                if role in ARCHITECTURE_RELEVANT_ROLES:
                    neighbor_roles.append(role)

            for in_node in reverse_graph.get(cls, set()):
                role = class_roles.get(in_node, "Other")
                if role in ARCHITECTURE_RELEVANT_ROLES:
                    neighbor_roles.append(role)

            if not neighbor_roles:
                continue

            unique_roles = sorted(set(neighbor_roles))
            if len(unique_roles) == 1:
                updated[cls] = unique_roles[0]

        class_roles = updated

    return class_roles


def simplify_graph_for_visualization(dep_graph):
    all_nodes = set(dep_graph.keys())
    in_degree = defaultdict(int)
    out_degree = defaultdict(int)

    for src, targets in dep_graph.items():
        out_degree[src] = len(targets)
        for dst in targets:
            all_nodes.add(dst)
            in_degree[dst] += 1

    if not all_nodes:
        return defaultdict(set)

    degree = {node: in_degree.get(node, 0) + out_degree.get(node, 0) for node in all_nodes}
    connected = [node for node in all_nodes if degree[node] > 0]
    ordered_nodes = sorted(connected if connected else all_nodes, key=lambda node: (-degree[node], node))
    selected = set(ordered_nodes[:GRAPH_VISUAL_MAX_NODES])

    simplified = defaultdict(set)
    for src in selected:
        ordered_targets = sorted(
            [dst for dst in dep_graph.get(src, set()) if dst in selected],
            key=lambda dst: (-degree.get(dst, 0), dst),
        )
        simplified[src] = set(ordered_targets[:GRAPH_VISUAL_MAX_OUT_EDGES_PER_NODE])

    return simplified


def infer_architecture_from_graph(class_roles, graph, project_metadata=None):
    project_metadata = project_metadata or {}
    has_app_entrypoint = bool(project_metadata.get("has_app_entrypoint", False))

    def runtime_role(name):
        if _is_protocol_container_name(name):
            return "Other"
        return class_roles.get(name, "Other")

    role_nodes = defaultdict(set)
    abstract_role_nodes = defaultdict(set)
    structural_edges = []

    for class_name in class_roles:
        role_name = runtime_role(class_name)
        if _class_is_architecture_relevant(class_name, role_name):
            role_nodes[role_name].add(class_name)
            abstract_role = ABSTRACT_ROLE_MAP.get(role_name)
            if abstract_role:
                abstract_role_nodes[abstract_role].add(class_name)

    for src, deps in graph.items():
        src_role = runtime_role(src)
        if not _class_is_architecture_relevant(src, src_role):
            continue
        for dst in deps:
            dst_role = runtime_role(dst)
            if not _class_is_architecture_relevant(dst, dst_role):
                continue
            if not _is_architecture_edge_allowed(src_role, dst_role):
                continue
            structural_edges.append((src, src_role, dst, dst_role))

    def edge_count(src_role, dst_role):
        return sum(1 for _, s_role, _, d_role in structural_edges if s_role == src_role and d_role == dst_role)

    def has_role(role_name):
        return len(role_nodes.get(role_name, set())) > 0

    def named_role_count(role_name, token):
        return sum(
            1
            for class_name in role_nodes.get(role_name, set())
            if token in set(_split_words(class_name))
        )

    view_to_viewmodel = edge_count("View", "ViewModel")
    viewmodel_to_view = edge_count("ViewModel", "View")
    viewmodel_to_domain = (
        edge_count("ViewModel", "Model")
        + edge_count("ViewModel", "Entity")
        + edge_count("ViewModel", "UseCase")
    )
    viewmodel_to_intent = edge_count("ViewModel", "Intent")
    viewmodel_to_state = edge_count("ViewModel", "State")

    view_to_presenter = edge_count("View", "Presenter")
    presenter_to_view = edge_count("Presenter", "View")
    presenter_to_domain = edge_count("Presenter", "Model") + edge_count("Presenter", "Entity")

    view_to_controller = edge_count("View", "Controller")
    controller_to_view = edge_count("Controller", "View")
    controller_to_domain = edge_count("Controller", "Model") + edge_count("Controller", "Entity")

    view_to_intent = edge_count("View", "Intent")
    intent_to_flow = (
        edge_count("Intent", "Model")
        + edge_count("Intent", "Entity")
        + edge_count("Intent", "UseCase")
        + edge_count("Intent", "Interactor")
        + edge_count("Intent", "ViewModel")
        + edge_count("Intent", "State")
    )
    intent_to_view = edge_count("Intent", "View")
    intent_to_state = edge_count("Intent", "State")
    state_role_nodes = named_role_count("Model", "state") + named_role_count("Entity", "state") + named_role_count("State", "state")
    reducer_role_nodes = (
        named_role_count("UseCase", "reducer")
        + named_role_count("Interactor", "reducer")
        + named_role_count("ViewModel", "reducer")
    )
    intent_node_count = len(role_nodes.get("Intent", set()))
    state_node_count = len(role_nodes.get("State", set()))
    viewmodel_node_count = len(role_nodes.get("ViewModel", set()))
    view_node_count = len(role_nodes.get("View", set()))
    intent_to_viewmodel = edge_count("Intent", "ViewModel")

    presenter_to_interactor = edge_count("Presenter", "Interactor")
    interactor_to_domain = edge_count("Interactor", "Model") + edge_count("Interactor", "Entity")
    presenter_to_router = edge_count("Presenter", "Router")

    contract_centric_viper = (
        has_role("Presenter")
        and has_role("Interactor")
        and has_role("Router")
        and presenter_to_view > 0
        and interactor_to_domain > 0
    )

    scores = {
        "MVVM": 0,
        "MVP": 0,
        "MVC": 0,
        "MVI": 0,
        "VIPER": 0,
    }

    # MVVM: View -> ViewModel -> Domain and ViewModel should not depend on View.
    scores["MVVM"] += view_to_viewmodel
    scores["MVVM"] += viewmodel_to_domain
    if view_to_viewmodel > 0 and viewmodel_to_domain > 0:
        scores["MVVM"] += 1
    if viewmodel_to_view > 0:
        scores["MVVM"] -= 1

    # MVP: View <-> Presenter and Presenter -> Domain.
    scores["MVP"] += view_to_presenter
    scores["MVP"] += presenter_to_view
    scores["MVP"] += presenter_to_domain
    if view_to_presenter > 0 and presenter_to_view > 0:
        scores["MVP"] += 1

    # MVC: View -> Controller -> Domain, optional Controller -> View.
    scores["MVC"] += view_to_controller
    scores["MVC"] += controller_to_domain
    scores["MVC"] += controller_to_view
    if view_to_controller > 0 and controller_to_domain > 0:
        scores["MVC"] += 1

    # MVI: View -> Intent plus event-to-state/domain flow, with unidirectional tendency.
    scores["MVI"] += view_to_intent
    scores["MVI"] += intent_to_flow
    scores["MVI"] += intent_to_state
    if state_role_nodes > 0:
        scores["MVI"] += 1
    if reducer_role_nodes > 0:
        scores["MVI"] += 1
    if view_to_intent > 0 and intent_to_view == 0:
        scores["MVI"] += 1

    # Some Compose-first MVI codebases use top-level @Composable functions as UI,
    # so class-based View nodes are sparse while the event/state loop is explicit
    # around ViewModel classes.
    sparse_class_views = view_node_count <= max(3, viewmodel_node_count // 3)
    mvi_event_state_edges = (
        viewmodel_to_intent
        + viewmodel_to_state
        + intent_to_viewmodel
        + intent_to_flow
        + intent_to_state
    )
    has_dense_event_state_loop = mvi_event_state_edges >= max(4, viewmodel_node_count // 2)
    intent_state_coverage = (
        intent_node_count >= max(2, viewmodel_node_count // 4)
        and state_node_count >= max(2, viewmodel_node_count // 4)
    )

    viewmodel_centric_mvi = (
        intent_node_count > 0
        and state_node_count > 0
        and has_dense_event_state_loop
        and (view_to_viewmodel > 0 or sparse_class_views)
        and (viewmodel_to_state > 0 or state_role_nodes > 0)
        and intent_state_coverage
    )
    if viewmodel_centric_mvi:
        scores["MVI"] += view_to_viewmodel
        scores["MVI"] += viewmodel_to_intent
        scores["MVI"] += viewmodel_to_state
        if viewmodel_node_count > 0 and intent_node_count >= max(1, viewmodel_node_count // 2):
            scores["MVI"] += 1
        if state_node_count >= max(1, viewmodel_node_count // 2):
            scores["MVI"] += 1

    # VIPER: View <-> Presenter, Presenter -> Interactor, Interactor -> Entity/Model, Presenter -> Router.
    scores["VIPER"] += view_to_presenter
    scores["VIPER"] += presenter_to_view
    scores["VIPER"] += presenter_to_interactor
    scores["VIPER"] += interactor_to_domain
    scores["VIPER"] += presenter_to_router
    if presenter_to_interactor > 0 and presenter_to_router > 0 and interactor_to_domain > 0:
        scores["VIPER"] += 1
    # Strong VIPER signal: Presenter uses both Interactor and Router layers
    if presenter_to_interactor > 0 and presenter_to_router > 0:
        scores["VIPER"] += 5
    # Contract-driven VIPER implementations can hide direct presenter->router/
    # presenter->interactor edges while preserving role boundaries.
    if contract_centric_viper:
        scores["VIPER"] += 2

    motifs = {
        "MVVM": view_to_viewmodel > 0 and viewmodel_to_domain > 0 and viewmodel_to_view == 0,
        "MVP": view_to_presenter > 0 and presenter_to_view > 0 and presenter_to_domain > 0,
        "MVC": view_to_controller > 0 and controller_to_domain > 0,
        "MVI": (
            (view_to_intent > 0 and (intent_to_flow > 0 or intent_to_state > 0 or state_role_nodes > 0 or reducer_role_nodes > 0))
            or viewmodel_centric_mvi
        ),
        "VIPER": (
            (presenter_to_interactor > 0 and presenter_to_router > 0 and interactor_to_domain > 0)
            or contract_centric_viper
        ),
    }

    if not has_app_entrypoint:
        return "unknown", scores, 0.0

    present_abstract_roles = [role for role, nodes in abstract_role_nodes.items() if nodes]
    if len(present_abstract_roles) <= 1:
        return "unknown", scores, 0.0

    has_ui = bool(abstract_role_nodes.get("UI"))
    has_presentation = bool(abstract_role_nodes.get("PresentationLogic"))
    has_domain = bool(abstract_role_nodes.get("DomainData"))
    
    # Require UI + (PresentationLogic OR DomainData) at minimum
    if not (has_ui and (has_presentation or has_domain)):
        return "unknown", scores, 0.0

    if not structural_edges and not (has_ui and has_domain and not has_presentation):
        return "unknown", scores, 0.0

    best_score = max(scores.values())
    
    # Fallback for minimal architectures: if all scores are 0 but basic roles present
    if best_score <= 0:
        has_view = bool(role_nodes.get("View"))
        has_viewmodel = bool(role_nodes.get("ViewModel"))
        has_presenter = bool(role_nodes.get("Presenter"))
        has_controller = bool(role_nodes.get("Controller"))
        has_model = bool(role_nodes.get("Model"))
        has_entity = bool(role_nodes.get("Entity"))
        has_domain = has_model or has_entity
        
        # Detect minimal architectures by role presence
        if has_view and has_domain:
            if has_controller:
                return "MVC", scores, 0.0
            elif has_viewmodel:
                return "MVVM", scores, 0.0
            elif has_presenter:
                return "MVP", scores, 0.0
            else:
                # Default to MVC for View + Model with minimal structure
                return "MVC", scores, 0.0
        
        return "unknown", scores, 0.0

    top_architectures = sorted([arch for arch, value in scores.items() if value == best_score])

    def best_motif_candidate(candidates):
        motif_candidates = [arch for arch in candidates if motifs.get(arch)]
        if not motif_candidates:
            return None
        if len(motif_candidates) == 1:
            return motif_candidates[0]

        ordered = sorted(motif_candidates, key=lambda arch: (-scores.get(arch, 0), arch))
        best = ordered[0]
        second = ordered[1]
        if (scores.get(best, 0) - scores.get(second, 0)) >= 2:
            return best
        return None

    def resolve_conflict(candidates):
        candidate_set = set(candidates)

        # Very strong VIPER signal: Interactor + Router present = VIPER pattern
        if "VIPER" in candidate_set:
            has_interactor = has_role("Interactor")
            has_router = has_role("Router")
            if has_interactor and has_router:
                # Check if edges exist
                if presenter_to_interactor > 0 or interactor_to_domain > 0 or presenter_to_router > 0:
                    if motifs["VIPER"]:
                        return "VIPER"

        if {"VIPER", "MVP"}.issubset(candidate_set):
            if has_role("Interactor") or has_role("Router"):
                if motifs["VIPER"] and (presenter_to_interactor > 0 or presenter_to_router > 0):
                    return "VIPER"
                if contract_centric_viper:
                    return "VIPER"
            if motifs["MVP"]:
                return "MVP"

        if {"VIPER", "MVI"}.issubset(candidate_set):
            if contract_centric_viper and scores.get("VIPER", 0) >= scores.get("MVI", 0):
                return "VIPER"
            if motifs["MVI"] and not contract_centric_viper:
                return "MVI"

        if {"MVI", "MVVM"}.issubset(candidate_set):
            if has_role("Intent") or reducer_role_nodes > 0 or state_role_nodes > 0:
                if motifs["MVI"]:
                    return "MVI"
            if motifs["MVVM"]:
                return "MVVM"

        if {"MVC", "MVP"}.issubset(candidate_set):
            if controller_to_view > 0 and motifs["MVC"]:
                return "MVC"
            if motifs["MVP"]:
                return "MVP"

        # Last resort: choose only if one motif candidate is clearly stronger.
        fallback = best_motif_candidate(candidates)
        if fallback is not None:
            return fallback

        return None

    def has_architecture_signature(architecture):
        if architecture == "MVVM":
            return (
                has_role("View")
                and has_role("ViewModel")
                and (has_role("Model") or has_role("Entity") or has_role("UseCase") or has_role("State"))
            )
        if architecture == "MVP":
            return (
                has_role("View")
                and has_role("Presenter")
                and (has_role("Model") or has_role("Entity") or has_role("Interactor") or has_role("UseCase"))
            )
        if architecture == "MVC":
            return (
                has_role("View")
                and has_role("Controller")
                and (has_role("Model") or has_role("Entity") or has_role("UseCase"))
            )
        if architecture == "MVI":
            return (
                (has_role("View") or has_role("ViewModel"))
                and (has_role("Intent") or has_role("State") or state_role_nodes > 0 or reducer_role_nodes > 0)
            )
        if architecture == "VIPER":
            return has_role("Presenter") and (has_role("Interactor") or has_role("Router"))
        return False

    def dominant_score_candidate(candidates=None):
        if candidates is None:
            candidates = [arch for arch, value in scores.items() if value > 0]
        unique_candidates = sorted(set(candidates), key=lambda arch: (-scores.get(arch, 0), arch))
        if not unique_candidates:
            return None

        best_arch = unique_candidates[0]
        best_arch_score = scores.get(best_arch, 0)
        second_score = scores.get(unique_candidates[1], 0) if len(unique_candidates) > 1 else 0
        if best_arch_score < 4:
            return None

        score_gap = best_arch_score - second_score
        ratio = (best_arch_score / second_score) if second_score > 0 else float("inf")
        if score_gap < 5 and ratio < 2.0:
            return None

        if not has_architecture_signature(best_arch):
            return None
        return best_arch

    if len(top_architectures) == 1:
        selected_arch = top_architectures[0]
        sorted_scores = sorted(scores.values(), reverse=True)
        second_score = sorted_scores[1] if len(sorted_scores) > 1 else 0
        contenders = [arch for arch, value in scores.items() if value > 0 and (best_score - value) <= 3]

        if not motifs.get(selected_arch):
            resolved = resolve_conflict(contenders if contenders else [selected_arch])
            if resolved is None:
                resolved = best_motif_candidate(contenders if contenders else [selected_arch])
            if resolved is None:
                resolved = dominant_score_candidate(contenders if contenders else [selected_arch])
            if resolved is None:
                return "unknown", scores, 0.0
            selected_arch = resolved

        # Trigger conflict resolution if scores are close (gap <= 3)
        if second_score > 0 and (best_score - second_score) <= 3:
            resolved = resolve_conflict(contenders)
            if resolved is not None:
                selected_arch = resolved
            elif len(contenders) > 1:
                # If no resolution but architecturally distinctive roles exist (e.g., Interactor+Router for VIPER),
                # use role-based heuristic
                has_interactor = bool(role_nodes.get("Interactor"))
                has_router = bool(role_nodes.get("Router"))
                if has_interactor and has_router and "VIPER" in contenders:
                    selected_arch = "VIPER"
                elif "MVP" in contenders and "VIPER" not in contenders:
                    selected_arch = "MVP"
                elif len(contenders) > 1:
                    fallback = dominant_score_candidate(contenders)
                    if fallback is not None:
                        selected_arch = fallback
                        total_positive = sum(max(0, value) for value in scores.values())
                        confidence = (max(0, scores[selected_arch]) / total_positive) if total_positive else 0.0
                        return selected_arch, scores, confidence
                    # Multiple close candidates but can't resolve: unknown
                    return "unknown", scores, 0.0

        total_positive = sum(max(0, value) for value in scores.values())
        confidence = (max(0, scores[selected_arch]) / total_positive) if total_positive else 0.0
        return selected_arch, scores, confidence

    resolved = resolve_conflict(top_architectures)
    if resolved is None:
        resolved = dominant_score_candidate()
    if resolved is None:
        return "unknown", scores, 0.0

    total_positive = sum(max(0, value) for value in scores.values())
    confidence = (max(0, scores[resolved]) / total_positive) if total_positive else 0.0
    return resolved, scores, confidence


def analyze_local_project(project_path):
    """Analyze a local Kotlin project directory."""
    if not os.path.exists(project_path):
        print(f"Project path not found: {project_path}")
        return False

    project_metadata = collect_project_metadata(project_path)
    if not project_metadata.get("has_app_entrypoint"):
        reason = project_metadata.get("entrypoint_reason", "no entrypoint indicators")
        print(f"Aviso: repositório sem sinais claros de app ({reason}).")

    class_roles, dep_graph = build_graph(project_path)

    print("Roles detectadas por classe:")
    for cls in sorted(class_roles):
        print(f"{cls} -> {class_roles[cls]}")

    architecture, counts, confidence = infer_architecture_from_graph(
        class_roles,
        dep_graph,
        project_metadata=project_metadata,
    )
    print("\nArquitetura inferida pelo grafo:", architecture)
    print("Padrões detectados:", counts)
    print(f"Confiança: {confidence:.2%}")
    return True


def parse_args():
    parser = argparse.ArgumentParser(
        description="Analyze a local Kotlin project or fetch GitHub repositories for batch analysis."
    )
    parser.add_argument(
        "project_path",
        nargs="?",
        default="./Pokedex",
        help="Local project path to analyze when not using --github.",
    )
    parser.add_argument(
        "--github",
        action="store_true",
        help="Run GitHub batch analysis instead of local project analysis.",
    )
    parser.add_argument(
        "--query",
        dest="queries",
        action="append",
        help="GitHub search query. Can be passed multiple times with --github.",
    )
    parser.add_argument(
        "--max-repos",
        type=int,
        default=0,
        help="Maximum number of GitHub repositories to analyze in batch mode. Use 0 for no limit.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=6,
        help="Parallel workers to use in GitHub batch mode.",
    )
    parser.add_argument(
        "--allow-libraries",
        action="store_true",
        help="Include library repositories in GitHub mode (disables app-entry filtering).",
    )
    parser.add_argument(
        "--github-token",
        type=str,
        default=None,
        help="GitHub API token. If omitted, uses GITHUB_TOKEN environment variable.",
    )
    parser.add_argument(
        "--disable-license-check",
        action="store_true",
        help="Disable license filtering in GitHub mode.",
    )
    return parser.parse_args()


def generate_graph_visualization(class_roles, dep_graph, output_path):
    """Generate a graph visualization image from dependency graph."""
    try:
        dep_graph = simplify_graph_for_visualization(dep_graph)
        G = nx.DiGraph()
        
        # Add nodes with role labels
        for node, deps in dep_graph.items():
            role = class_roles.get(node, "Unknown")
            G.add_node(node, role=role)
            for dep in deps:
                G.add_edge(node, dep)
        
        # Color map for roles
        role_colors = {
            "View": "#FF6B6B",
            "ViewModel": "#4ECDC4",
            "Presenter": "#45B7D1",
            "Controller": "#FFA07A",
            "Repository": "#98D8C8",
            "Service": "#F7DC6F",
            "UseCase": "#BB8FCE",
            "Model": "#85C1E2",
            "Entity": "#F8B88B",
            "Intent": "#A9DFBF",
            "Router": "#D5A6BD",
            "DI": "#F9E79F",
            "Test": "#D3D3D3",
            "Unknown": "#808080"
        }
        
        node_colors = [role_colors.get(class_roles.get(node, "Unknown"), "#808080") for node in G.nodes()]
        
        plt.figure(figsize=(14, 10))
        pos = nx.spring_layout(G, k=2, iterations=50, seed=42)
        
        nx.draw_networkx_nodes(G, pos, node_color=node_colors, node_size=500, alpha=0.8)
        nx.draw_networkx_edges(G, pos, edge_color='gray', arrows=True, arrowsize=15, alpha=0.5)
        
        labels = {node: node[:20] for node in G.nodes()}
        nx.draw_networkx_labels(G, pos, labels, font_size=8)
        
        plt.title("Dependency Graph", fontsize=16, fontweight='bold')
        plt.axis('off')
        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
        
    except Exception as e:
        print(f"Error generating graph visualization: {e}")


def map_files_to_roles(class_roles, repo_files, repo_root=None):
    role_files = defaultdict(list)
    classification_methods = defaultdict(int)
    
    for file_path, content in repo_files.items():
        role = None
        method = None
        path_l = file_path.lower()

        uri_role, uri_method, uri_scores = score_file_role_from_uri(file_path, repo_root=repo_root)
        uri_confident = _is_confident_uri_role(uri_role, uri_method, uri_scores)
        if uri_confident and "test" in path_l and uri_role != "Test":
            uri_confident = False
        if uri_confident and uri_method == "uri_folder":
            role = uri_role
            method = "uri_folder"
        elif uri_confident and uri_method == "common_words":
            role = uri_role
            method = "uri_common_words"

        file_stem = os.path.splitext(os.path.basename(file_path))[0]
        if role is None and file_stem in class_roles:
            role = class_roles[file_stem]
            method = "class_file_name"
        
        if role is None:
            try:
                file_classes = parse_kotlin_file(content)
            except Exception:
                file_classes = {}

            for class_name in file_classes.keys():
                if class_name in class_roles:
                    role = class_roles[class_name]
                    method = "class_declaration"
                    break 

        if role is None:
            role = "Other"
            method = "unclassified"

        role_files[role].append(file_path)
        classification_methods[method] += 1
    
    return dict(role_files), dict(classification_methods)


def load_json_object(path, default=None):
    if default is None:
        default = {}
    if not os.path.exists(path):
        return dict(default)

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        try:
            text = read_file(path)
            obj, _ = json.JSONDecoder().raw_decode(text)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    except Exception:
        pass

    return dict(default)


def update_summary_json(owner, repo, repo_hash, architecture):
    """Update the global summary.json with analyzed repository info."""
    summary_path = os.path.join(BASE_OUTPUT_DIR, "summary.json")

    summary = load_json_object(summary_path, default={})
    
    if architecture not in summary:
        summary[architecture] = {}
    
    summary[architecture][repo_hash] = {
        "owner": owner,
        "reponame": repo
    }
    
    with open(summary_path, 'w') as f:
        json.dump(summary, f, indent=4)


def analyze_repo(owner, repo, require_app_entry=False, require_allowed_license=True, license_spdx=None):
    repo_hash = hashlib.md5(f"{owner}/{repo}".encode()).hexdigest()
    repo_dir = os.path.join(BASE_OUTPUT_DIR, repo_hash)
    analysis_path = os.path.join(repo_dir, "analysis.json")
    resolved_license = normalize_spdx_id(license_spdx)

    if os.path.exists(analysis_path):
        print(f"Skipping {owner}/{repo} (already analyzed)")
        return "already_analyzed"

    if require_allowed_license:
        if not resolved_license or resolved_license in {"NOASSERTION", "NONE"}:
            resolved_license = normalize_spdx_id(fetch_repository_license(owner, repo))
        allowed, license_reason = is_license_allowed(resolved_license)
        if not allowed:
            print(f"Skipping {owner}/{repo} (license not allowed: {license_reason})")
            return "skipped_license"
    elif not resolved_license:
        resolved_license = normalize_spdx_id(fetch_repository_license(owner, repo))

    os.makedirs(repo_dir, exist_ok=True)

    clone_path = clone_repository(owner, repo, repo_dir)
    if not clone_path:
        print(f"Failed to clone {owner}/{repo}")
        return "clone_failed"

    repo_files = load_repo_files(clone_path)
    if not repo_files:
        print(f"No Kotlin files found in {owner}/{repo}")
        shutil.rmtree(repo_dir, ignore_errors=True)
        return "no_kotlin"

    if require_app_entry:
        looks_like_app, reason = repository_looks_like_application(clone_path, repo_files=repo_files)
        if not looks_like_app:
            print(f"Skipping {owner}/{repo} (likely library repo: {reason})")
            shutil.rmtree(repo_dir, ignore_errors=True)
            return "skipped_library"

    try:
        project_metadata = collect_project_metadata(clone_path)
        class_roles, dep_graph = build_graph(clone_path)
        architecture, counts, confidence = infer_architecture_from_graph(
            class_roles,
            dep_graph,
            project_metadata=project_metadata,
        )
        
        role_files, classification_methods = map_files_to_roles(
            class_roles,
            repo_files,
            repo_root=clone_path,
        )

        result = {
            "repo_url": f"https://github.com/{owner}/{repo}",
            "license_spdx": resolved_license,
            "has_app_entrypoint": project_metadata.get("has_app_entrypoint", False),
            "entrypoint_reason": project_metadata.get("entrypoint_reason", ""),
            "architecture": architecture,
            "confidence": confidence,
            "pattern_counts": counts,
            "total_classes": len(class_roles),
            "total_dependencies": sum(len(deps) for deps in dep_graph.values()),
            "role_distribution": {role: sum(1 for r in class_roles.values() if r == role) for role in set(class_roles.values())},
            "files_by_role": role_files,
            "file_classification_methods": classification_methods,
        }

        with open(analysis_path, "w") as f:
            json.dump(result, f, indent=4)

        graph_image_path = os.path.join(repo_dir, "dependency_graph.png")
        generate_graph_visualization(class_roles, dep_graph, graph_image_path)

        update_summary_json(owner, repo, repo_hash, architecture)

        print(f"✓ {owner}/{repo} -> {architecture} (confidence: {confidence:.2%})")
        return "analyzed"

    except Exception as e:
        print(f"✗ Error analyzing {owner}/{repo}: {e}")
        return "error"


def fetch_repositories(query, max_results=100):
    """Fetch repositories from GitHub API based on search query.

    max_results:
        - positive int: fetch up to that many repositories
        - 0 or None: fetch up to GitHub Search API cap (1000)
    """
    try:
        # GitHub Search API returns at most 1000 results per query.
        target = 1000 if not max_results or max_results <= 0 else max_results
        per_page = 100
        max_pages = 10
        page = 1
        repos = []

        while len(repos) < target and page <= max_pages:
            remaining = target - len(repos)
            page_size = min(per_page, remaining)
            url = (
                "https://api.github.com/search/repositories"
                f"?q={query}&per_page={page_size}&page={page}&sort=stars&order=desc"
            )
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                print(f"GitHub API error ({resp.status_code}) for query '{query}' page {page}")
                break

            items = resp.json().get("items", [])
            if not items:
                break

            for item in items:
                full = item.get("full_name", "")
                if "/" in full:
                    owner, repo = full.split("/", 1)
                    license_info = item.get("license") or {}
                    stars = item.get("stargazers_count") or 0
                    repos.append((owner, repo, license_info.get("spdx_id"), int(stars)))

            if len(items) < page_size:
                break
            page += 1

        return repos
    except Exception as e:
        print(f"Error fetching repositories: {e}")
        return []


# ==========================================================
# PARALLEL EXECUTION
# ==========================================================

def run_analysis(queries=None, max_workers=6, max_repos=0, require_app_entry=True, require_allowed_license=True):
    """Run analysis on repositories from GitHub search.
    
    Args:
        queries: List of GitHub search queries
        max_workers: Number of parallel workers
        max_repos: Maximum number of repositories to analyze (default: 0 = no limit)
        require_app_entry: Skip likely library repos when True
        require_allowed_license: Skip repos whose license is not in allowed SPDX list
    """
    if queries is None:
        queries = [
            "topic:MVVM language:kotlin",
            "topic:MVI language:kotlin",
            "language:kotlin topic:VIPER",
            "language:kotlin topic:MVP",
            "language:kotlin topic:MVC"
        ]

    fetched = []
    for q in queries:
        print(f"Searching for: {q}")
        fetched.extend(fetch_repositories(q, max_results=max_repos))

    # Deduplicate by full name while preserving first-seen order.
    # Prefer entries with a concrete SPDX id over missing/NOASSERTION.
    repos_by_name = {}
    for owner, repo, spdx, stars in fetched:
        key = (owner, repo)
        if key not in repos_by_name:
            repos_by_name[key] = {
                "spdx": spdx,
                "stars": int(stars or 0),
            }
            continue
        current = normalize_spdx_id(repos_by_name[key]["spdx"])
        incoming = normalize_spdx_id(spdx)
        if current in {"", "NOASSERTION", "NONE"} and incoming not in {"", "NOASSERTION", "NONE"}:
            repos_by_name[key]["spdx"] = spdx
        repos_by_name[key]["stars"] = max(repos_by_name[key]["stars"], int(stars or 0))

    repos = [
        (owner, repo, meta["spdx"], meta["stars"])
        for (owner, repo), meta in repos_by_name.items()
    ]
    repos.sort(key=lambda item: item[3], reverse=True)
    
    if max_repos and max_repos > 0:
        repos = repos[:max_repos]
        limit_text = f"limited to {max_repos}"
    else:
        limit_text = "no repo limit (GitHub search cap per query applies)"
    
    mode = "apps only" if require_app_entry else "apps + libraries"
    license_mode = "license-check on" if require_allowed_license else "license-check off"
    print(f"Analyzing {len(repos)} repositories ({limit_text}, mode: {mode}, {license_mode})")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        status_counts = defaultdict(int)
        for owner, repo, spdx, _stars in repos:
            futures.append(
                executor.submit(
                    analyze_repo,
                    owner,
                    repo,
                    require_app_entry,
                    require_allowed_license,
                    spdx,
                )
            )

        completed = 0
        for future in concurrent.futures.as_completed(futures):
            completed += 1
            try:
                status = future.result()
                if status is None:
                    status = "unknown"
            except Exception as e:
                status = "worker_exception"
                print(f"✗ Worker exception: {e}")
            status_counts[status] += 1
            print(f"Progress: {completed}/{len(futures)}")

    print(
        "Run summary: "
        f"analyzed={status_counts['analyzed']}, "
        f"already_analyzed={status_counts['already_analyzed']}, "
        f"skipped_license={status_counts['skipped_license']}, "
        f"skipped_library={status_counts['skipped_library']}, "
        f"no_kotlin={status_counts['no_kotlin']}, "
        f"clone_failed={status_counts['clone_failed']}, "
        f"errors={status_counts['error'] + status_counts['worker_exception']}"
    )
    if status_counts["analyzed"] == 0:
        print("No analysis.json was created in this run. Try --allow-libraries, --disable-license-check, increase --max-repos, or adjust --query.")


def main():
    args = parse_args()

    if args.github:
        configure_github_token(args.github_token)
        if not GITHUB_TOKEN:
            print("⚠️  No GitHub token found. Using unauthenticated access (60 requests/hour).")
            print("   Set GITHUB_TOKEN or use --github-token to avoid API limits.")

        run_analysis(
            queries=args.queries,
            max_workers=args.max_workers,
            max_repos=args.max_repos,
            require_app_entry=not args.allow_libraries,
            require_allowed_license=not args.disable_license_check,
        )
        return

    analyze_local_project(args.project_path)


if __name__ == "__main__":
    main()

# 🧠 Kotlin Architecture Analyzer

A static analysis tool designed to automatically **infer software architecture patterns** (e.g., MVVM, MVP, MVC, MVI, VIPER) from Kotlin-based projects. This tool analyzes source code structure, class roles, and dependencies to identify architectural styles in both **local projects** and **GitHub repositories**.

---

## 🚀 Features

* 🔍 **Automatic Architecture Detection**

  * Supports: MVVM, MVP, MVC, MVI, VIPER
  * Provides confidence score and pattern metrics

* 🧩 **Role Classification**

  * Identifies roles such as:

    * View, ViewModel, Presenter, Controller
    * Repository, Service, UseCase, Entity, Intent, etc.

* 📊 **Dependency Graph Analysis**

  * Builds and refines class dependency graphs
  * Filters architecture-relevant interactions

* 🌐 **GitHub Mining**

  * Fetches repositories via GitHub API
  * Filters by license and project type
  * Batch analysis with parallel processing

* 🖼️ **Graph Visualization**

  * Generates dependency graph images (`.png`)

* 📁 **Detailed Output**

  * JSON reports per repository
  * Global summary aggregation

---

## 📦 Requirements

* Python 3.8+

* Dependencies:

  ```bash
  pip install tree-sitter gitpython requests networkx matplotlib
  ```

* Tree-sitter Kotlin grammar:

  * Clone and build:

    ```bash
    git clone https://github.com/fwcd/tree-sitter-kotlin
    ```
  * The script will compile `tree-sitter-kotlin.so` automatically if needed

---

## 🛠️ Usage

### 🔹 Analyze a Local Project

```bash
python analyzer.py /path/to/your/kotlin/project
```

Default:

```bash
python analyzer.py
```

(Uses `./Pokedex` as default path)

---

### 🔹 Analyze GitHub Repositories

```bash
python analyzer.py --github
```

#### Custom queries:

```bash
python analyzer.py --github \
  --query "language:kotlin mvvm" \
  --query "language:kotlin mvi"
```

#### Limit repositories:

```bash
python analyzer.py --github --max-repos 50
```

#### Use multiple workers:

```bash
python analyzer.py --github --max-workers 8
```

---

## 🔐 GitHub Token (Optional)

To avoid rate limits:

```bash
export GITHUB_TOKEN=your_token_here
```

Or:

```bash
python analyzer.py --github --github-token your_token_here
```

---

## 📊 Output Structure

```
kmp_analysis/
├── summary.json
├── <repo_hash>/
│   ├── analysis.json
│   ├── dependency_graph.png
│   └── repo/ (cloned repository)
```

---

## 📄 Example Output (`analysis.json`)

```json
{
  "architecture": "MVVM",
  "confidence": 0.87,
  "total_classes": 120,
  "total_dependencies": 340,
  "role_distribution": {
    "View": 15,
    "ViewModel": 10,
    "Model": 30
  }
}
```

---

## 🧠 How It Works

1. **File Extraction & Parsing**

   * Uses Tree-sitter to parse Kotlin code

2. **Class Role Identification**

   * Heuristics based on:

     * Naming conventions
     * Annotations
     * Imports
     * Inheritance

3. **Dependency Graph Construction**

   * Builds graph from:

     * Imports
     * Symbols
     * Inheritance

4. **Architecture Inference**

   * Scores architectural patterns
   * Resolves conflicts using structural motifs

---

## ⚙️ Configuration Options

| Argument                  | Description                 |
| ------------------------- | --------------------------- |
| `--github`                | Enable GitHub analysis mode |
| `--query`                 | GitHub search query         |
| `--max-repos`             | Limit number of repos       |
| `--max-workers`           | Parallel processing         |
| `--allow-libraries`       | Include non-app repos       |
| `--disable-license-check` | Skip license filtering      |

---

## ⚠️ Limitations

* Heuristic-based → may not be 100% accurate
* Depends on naming conventions and project structure
* Large projects may increase processing time

---

## 📚 Use Cases

* Empirical software architecture research
* Dataset generation for ML models
* Codebase auditing and documentation
* Comparative analysis of architectural patterns

---

## 🤝 Contributing

Contributions are welcome! Feel free to:

* Open issues
* Submit pull requests
* Suggest improvements to heuristics or detection logic

---

## 📜 License

This project is intended for research and analysis purposes.
Make sure to respect the licenses of analyzed repositories.

---

If you want, I can also:

* tailor this README for a **paper artifact (ACM/IEEE style)**
* add **badges + screenshots**
* or generate a **short version for GitHub landing page**

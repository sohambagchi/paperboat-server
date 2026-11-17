"""Canonical venue names, so one venue groups as one venue whichever source named it.

DBLP says "NeurIPS", OpenAlex "Neural Information Processing Systems", Crossref "Advances
in Neural Information Processing Systems 36". All of them become the short name.
Unknown venues pass through unchanged. Add or override entries in
<data dir>/venues.json: {"Short Name": ["alias", "another alias"], ...}.
"""

import json
import logging
import re

from . import config
from .util import ascii_fold

log = logging.getLogger("paperboat.venues")

# Canonical name -> aliases. The canonical name is itself an alias.
VENUES: dict[str, list[str]] = {
    # preprints
    "arXiv": ["CoRR", "arXiv.org", "ArXiv", "arXiv (Cornell University)"],
    # machine learning, vision, language
    "NeurIPS": ["NIPS", "Neural Information Processing Systems",
                "Advances in Neural Information Processing Systems",
                "Conference on Neural Information Processing Systems"],
    "ICML": ["International Conference on Machine Learning"],
    "ICLR": ["International Conference on Learning Representations"],
    "AAAI": ["AAAI Conference on Artificial Intelligence",
             "Proceedings of the AAAI Conference on Artificial Intelligence"],
    "IJCAI": ["International Joint Conference on Artificial Intelligence"],
    "AISTATS": ["International Conference on Artificial Intelligence and Statistics"],
    "UAI": ["Conference on Uncertainty in Artificial Intelligence", "Uncertainty in Artificial Intelligence"],
    "COLT": ["Conference on Learning Theory", "Annual Conference on Computational Learning Theory"],
    "CVPR": ["Computer Vision and Pattern Recognition",
             "IEEE/CVF Conference on Computer Vision and Pattern Recognition"],
    "ICCV": ["International Conference on Computer Vision", "IEEE/CVF International Conference on Computer Vision"],
    "ECCV": ["European Conference on Computer Vision"],
    "ACL": ["Annual Meeting of the Association for Computational Linguistics",
            "Meeting of the Association for Computational Linguistics"],
    "EMNLP": ["Empirical Methods in Natural Language Processing",
              "Conference on Empirical Methods in Natural Language Processing"],
    "NAACL": ["North American Chapter of the Association for Computational Linguistics",
              "NAACL-HLT"],
    "TACL": ["Transactions of the Association for Computational Linguistics", "Trans. Assoc. Comput. Linguistics"],
    "JMLR": ["Journal of Machine Learning Research", "J. Mach. Learn. Res."],
    "TMLR": ["Transactions on Machine Learning Research", "Trans. Mach. Learn. Res."],
    "TPAMI": ["IEEE Transactions on Pattern Analysis and Machine Intelligence",
              "IEEE Trans. Pattern Anal. Mach. Intell."],
    "KDD": ["Knowledge Discovery and Data Mining", "SIGKDD",
            "ACM SIGKDD International Conference on Knowledge Discovery and Data Mining"],
    "WWW": ["The Web Conference", "World Wide Web Conference", "International World Wide Web Conference"],
    "SIGIR": ["International ACM SIGIR Conference on Research and Development in Information Retrieval"],
    # systems, networking, databases
    "OSDI": ["Operating Systems Design and Implementation",
             "USENIX Symposium on Operating Systems Design and Implementation"],
    "SOSP": ["Symposium on Operating Systems Principles", "ACM Symposium on Operating Systems Principles"],
    "NSDI": ["Networked Systems Design and Implementation",
             "USENIX Symposium on Networked Systems Design and Implementation"],
    "EuroSys": ["European Conference on Computer Systems"],
    "USENIX ATC": ["USENIX Annual Technical Conference", "USENIX ATC"],
    "FAST": ["File and Storage Technologies", "USENIX Conference on File and Storage Technologies"],
    "SIGCOMM": ["ACM SIGCOMM Conference", "Conference of the ACM Special Interest Group on Data Communication"],
    "SC": ["International Conference for High Performance Computing, Networking, Storage and Analysis",
           "Supercomputing"],
    "PPoPP": ["Principles and Practice of Parallel Programming",
              "ACM SIGPLAN Symposium on Principles and Practice of Parallel Programming"],
    "SPAA": ["Symposium on Parallelism in Algorithms and Architectures",
             "ACM Symposium on Parallelism in Algorithms and Architectures"],
    "PODC": ["Principles of Distributed Computing", "ACM Symposium on Principles of Distributed Computing"],
    "DISC": ["International Symposium on Distributed Computing"],
    "VLDB": ["PVLDB", "Proc. VLDB Endow.", "Proceedings of the VLDB Endowment", "Very Large Data Bases"],
    "SIGMOD": ["Management of Data", "International Conference on Management of Data",
               "Proc. ACM Manag. Data", "Proceedings of the ACM on Management of Data"],
    "ICDE": ["International Conference on Data Engineering", "IEEE International Conference on Data Engineering"],
    # architecture
    "ASPLOS": ["Architectural Support for Programming Languages and Operating Systems",
               "International Conference on Architectural Support for Programming Languages and Operating Systems"],
    "ISCA": ["International Symposium on Computer Architecture",
             "Annual International Symposium on Computer Architecture"],
    "MICRO": ["International Symposium on Microarchitecture", "IEEE/ACM International Symposium on Microarchitecture",
              "Annual IEEE/ACM International Symposium on Microarchitecture"],
    "HPCA": ["High-Performance Computer Architecture",
             "IEEE International Symposium on High-Performance Computer Architecture",
             "International Symposium on High Performance Computer Architecture"],
    "PACT": ["Parallel Architectures and Compilation Techniques",
             "International Conference on Parallel Architectures and Compilation Techniques"],
    # programming languages, verification, software engineering
    "PLDI": ["Programming Language Design and Implementation",
             "ACM SIGPLAN Conference on Programming Language Design and Implementation",
             "ACM-SIGPLAN Symposium on Programming Language Design and Implementation"],
    "POPL": ["Principles of Programming Languages", "Symposium on Principles of Programming Languages",
             "ACM SIGPLAN-SIGACT Symposium on Principles of Programming Languages"],
    "OOPSLA": ["Object-Oriented Programming, Systems, Languages, and Applications",
               "Object-Oriented Programming Systems, Languages, and Applications", "OOPSLA/SPLASH"],
    "ICFP": ["International Conference on Functional Programming"],
    "PACMPL": ["Proc. ACM Program. Lang.", "Proceedings of the ACM on Programming Languages"],
    "TOPLAS": ["ACM Transactions on Programming Languages and Systems", "ACM Trans. Program. Lang. Syst."],
    "ECOOP": ["European Conference on Object-Oriented Programming"],
    "CGO": ["Code Generation and Optimization", "International Symposium on Code Generation and Optimization"],
    "CC": ["International Conference on Compiler Construction", "Compiler Construction"],
    "CAV": ["Computer Aided Verification", "International Conference on Computer Aided Verification"],
    "TACAS": ["Tools and Algorithms for the Construction and Analysis of Systems"],
    "ICSE": ["International Conference on Software Engineering"],
    "FSE": ["ESEC/FSE", "ESEC/SIGSOFT FSE", "Foundations of Software Engineering",
            "Proc. ACM Softw. Eng.", "Proceedings of the ACM on Software Engineering"],
    "ASE": ["Automated Software Engineering", "IEEE/ACM International Conference on Automated Software Engineering"],
    "ISSTA": ["International Symposium on Software Testing and Analysis"],
    # security
    "IEEE S&P": ["IEEE Symposium on Security and Privacy", "Symposium on Security and Privacy", "S&P", "SP"],
    "CCS": ["Computer and Communications Security", "ACM Conference on Computer and Communications Security",
            "ACM SIGSAC Conference on Computer and Communications Security"],
    "USENIX Security": ["USENIX Security Symposium"],
    "NDSS": ["Network and Distributed System Security Symposium"],
    # theory
    "STOC": ["Symposium on Theory of Computing", "ACM Symposium on Theory of Computing"],
    "FOCS": ["Foundations of Computer Science", "IEEE Symposium on Foundations of Computer Science",
             "IEEE Annual Symposium on Foundations of Computer Science"],
    "SODA": ["Symposium on Discrete Algorithms", "ACM-SIAM Symposium on Discrete Algorithms"],
    "LICS": ["Logic in Computer Science", "ACM/IEEE Symposium on Logic in Computer Science"],
    # HCI
    "CHI": ["Human Factors in Computing Systems", "CHI Conference on Human Factors in Computing Systems"],
    "UIST": ["User Interface Software and Technology", "ACM Symposium on User Interface Software and Technology"],
    # journals and magazines
    "JACM": ["Journal of the ACM", "J. ACM"],
    "CACM": ["Communications of the ACM", "Commun. ACM"],
    "TOCS": ["ACM Transactions on Computer Systems", "ACM Trans. Comput. Syst."],
    "Nature": [],
    "Science": [],
}

# Words that vary between sources without changing which venue is meant.
# MARKERS say "this is a meeting": "International Conference on Machine Learning" is
# ICML, but the journal "Machine Learning" is not, so a name matched only by dropping
# its markers must have markers of its own.
MARKERS = {"proceedings", "proc", "conference", "conf", "symposium", "symp", "meeting"}
FILLER = MARKERS | {
    "of", "the", "in", "on", "and", "for", "annual", "international", "intl",
    "acm", "ieee", "usenix", "sigplan", "sigact",
    "sigops", "sigarch", "sigsac", "sigsoft", "sigkdd", "siam", "cvf", "ieee/cvf", "ieee/acm", "acm/ieee",
    "th", "st", "nd", "rd", "edition", "volume", "vol", "part",
}


def _norm(s: str) -> tuple[str, bool]:
    """(distinctive words, whether the name had a meeting marker)."""
    s = ascii_fold(s).lower().replace("&", " and ")
    s = re.sub(r"\b\d+(st|nd|rd|th)?\b|'\d\d\b", " ", s)  # ordinals, years, '24
    words = re.findall(r"[a-z]+", s)
    return " ".join(w for w in words if w not in FILLER), any(w in MARKERS for w in words)


# normalized words -> (canonical name, every alias with these words had a marker)
_index: dict[str, tuple[str, bool]] | None = None


def _load() -> dict[str, tuple[str, bool]]:
    global _index
    if _index is None:
        table = dict(VENUES)
        extra = config.DATA_DIR / "venues.json"
        if extra.exists():
            try:
                table.update(json.loads(extra.read_text()))
            except (OSError, ValueError) as e:
                log.warning("ignoring %s: %s", extra, e)
        _index = {}
        for name, aliases in table.items():
            for alias in [name, *aliases]:
                key, marked = _norm(alias)
                if not key:
                    continue
                prev = _index.get(key)
                _index[key] = (prev[0], prev[1] and marked) if prev else (name, marked)
    return _index


def _lookup(s: str) -> str | None:
    key, marked = _norm(s)
    hit = _load().get(key)
    if hit and (marked or not hit[1]):
        return hit[0]
    return None


def reset() -> None:
    """Forget the loaded table (after the data directory or venues.json changes)."""
    global _index
    _index = None


def canonical(venue: str | None) -> str | None:
    """The short name of a known venue, else the venue as given."""
    if not venue or not venue.strip():
        return venue
    venue = venue.strip()
    if hit := _lookup(venue):
        return hit
    if re.search(r"workshop", venue, re.I):  # "Workshop on X (at PLDI)" is not PLDI
        return venue
    # "Proceedings of the 45th ... (PLDI '24)": trust a known acronym in parentheses.
    for m in re.finditer(r"\(([^()]+)\)", venue):
        if hit := _lookup(m.group(1)):
            return hit
    # "PLDI '24: Proceedings of ...": a known name before a colon.
    m = re.match(r"([A-Za-z&]+(?:\s[A-Za-z&]+)?)\s*(?:'?\d{2,4})?\s*:", venue)
    if m and (hit := _lookup(m.group(1))):
        return hit
    return venue

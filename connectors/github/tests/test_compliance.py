"""Framework compliance for GitHubRepoSource and GitHubToolkit."""

from aisquare.pipe.testing.compliance import (
    connector_compliance_suite,
    toolkit_compliance_suite,
)

from aisquare_pipe_github.connector import GitHubRepoSource
from aisquare_pipe_github.toolkit import GitHubToolkit


class TestGitHubRepoSourceCompliance(connector_compliance_suite(GitHubRepoSource)):
    pass


class TestGitHubToolkitCompliance(toolkit_compliance_suite(GitHubToolkit)):
    pass

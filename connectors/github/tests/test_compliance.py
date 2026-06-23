"""Framework compliance for GitHubRepoSource."""

from aisquare.pipe.testing.compliance import connector_compliance_suite

from aisquare_pipe_github.connector import GitHubRepoSource


class TestGitHubRepoSourceCompliance(connector_compliance_suite(GitHubRepoSource)):
    pass

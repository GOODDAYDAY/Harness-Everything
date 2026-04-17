"""Tests for harness.core.checkpoint module."""

import json
import tempfile
from pathlib import Path
from datetime import datetime

import pytest

from harness.core.checkpoint import CheckpointManager, CheckpointMetadata
from harness.core.artifacts import ArtifactStore


class TestCheckpointManager:
    """Test CheckpointManager functionality."""
    
    def setup_method(self):
        """Create a temporary directory and ArtifactStore for each test."""
        self.temp_dir = tempfile.mkdtemp()
        self.store = ArtifactStore(self.temp_dir, "test_run")
        self.checkpoint = CheckpointManager(self.store)
    
    def test_write_checkpoint_metadata_validates_score_range(self):
        """Test that write_checkpoint_metadata validates synthesis_specificity_score range."""
        metadata = CheckpointMetadata(
            checkpoint_type="phase",
            outer_round=1,
            phase_label="test_phase",
            inner_index=0,
            basic_score=0.8,
            diffusion_score=0.7,
            critique_count=3,
            actionable_critiques=2,
            synthesis_specificity_score=11,  # Invalid: should be 0-10
            timestamp=datetime.now()
        )
        
        with pytest.raises(ValueError) as exc_info:
            self.checkpoint.write_checkpoint_metadata(metadata, "round_1", "phase_test")
        
        assert "synthesis_specificity_score must be between 0 and 10" in str(exc_info.value)
        assert "got 11" in str(exc_info.value)
    
    def test_write_checkpoint_metadata_validates_score_range_lower_bound(self):
        """Test that write_checkpoint_metadata validates lower bound of synthesis_specificity_score."""
        metadata = CheckpointMetadata(
            checkpoint_type="phase",
            outer_round=1,
            phase_label="test_phase",
            inner_index=0,
            basic_score=0.8,
            diffusion_score=0.7,
            critique_count=3,
            actionable_critiques=2,
            synthesis_specificity_score=-1,  # Invalid: should be 0-10
            timestamp=datetime.now()
        )
        
        with pytest.raises(ValueError) as exc_info:
            self.checkpoint.write_checkpoint_metadata(metadata, "round_1", "phase_test")
        
        assert "synthesis_specificity_score must be between 0 and 10" in str(exc_info.value)
        assert "got -1" in str(exc_info.value)
    
    def test_write_checkpoint_metadata_accepts_valid_scores(self):
        """Test that write_checkpoint_metadata accepts valid synthesis_specificity_score values."""
        for score in [0, 5, 10]:
            metadata = CheckpointMetadata(
                checkpoint_type="phase",
                outer_round=1,
                phase_label="test_phase",
                inner_index=0,
                basic_score=0.8,
                diffusion_score=0.7,
                critique_count=3,
                actionable_critiques=2,
                synthesis_specificity_score=score,
                timestamp=datetime.now()
            )
            
            # Should not raise
            self.checkpoint.write_checkpoint_metadata(metadata, "round_1", "phase_test")
            
            # Clean up for next iteration
            json_path = self.store.path("round_1", "phase_test", "checkpoint_metadata.json")
            if json_path.exists():
                json_path.unlink()
    
    def test_path_validation_occurs_before_score_validation(self):
        """Test that path validation occurs before score validation to prevent security bypass."""
        # Create metadata with invalid score (11) and malicious path segment ("..")
        metadata = CheckpointMetadata(
            checkpoint_type="phase",
            outer_round=1,
            phase_label="test_phase",
            inner_index=0,
            basic_score=0.8,
            diffusion_score=0.7,
            critique_count=3,
            actionable_critiques=2,
            synthesis_specificity_score=11,  # Invalid score
            timestamp=datetime.now()
        )
        
        # Try to write with directory traversal attempt
        with pytest.raises(ValueError) as exc_info:
            self.checkpoint.write_checkpoint_metadata(metadata, "round_1", "..", "phase_test")
        
        # Should raise path validation error, not score validation error
        error_msg = str(exc_info.value)
        assert "Path segment '..' not allowed" in error_msg
        assert "synthesis_specificity_score must be between 0 and 10" not in error_msg
    
    def test_read_checkpoint_metadata_handles_corrupted_json(self):
        """Test that read_checkpoint_metadata returns None when JSON is corrupted."""
        # Write corrupted JSON
        json_path = self.store.path("round_1", "phase_test", "checkpoint_metadata.json")
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text("{invalid json", encoding="utf-8")
        
        # Should return None, not raise
        result = self.checkpoint.read_checkpoint_metadata("round_1", "phase_test")
        assert result is None
    
    def test_read_checkpoint_metadata_handles_missing_keys(self):
        """Test that read_checkpoint_metadata returns None when JSON has missing required keys."""
        # Write JSON missing required fields
        json_path = self.store.path("round_1", "phase_test", "checkpoint_metadata.json")
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text('{"checkpoint_type": "phase"}', encoding="utf-8")
        
        # Should return None, not raise
        result = self.checkpoint.read_checkpoint_metadata("round_1", "phase_test")
        assert result is None
    
    def test_validate_path_segments_blocks_directory_traversal(self):
        """Test that _validate_path_segments blocks directory traversal attempts."""
        # Test with ".." segment
        with pytest.raises(ValueError) as exc_info:
            self.checkpoint._validate_path_segments("round_1", "..", "phase_test")
        
        assert "Path segment '..' not allowed" in str(exc_info.value)
    
    def test_validate_path_segments_blocks_escape_attempts(self):
        """Test that _validate_path_segments blocks attempts to escape artifact store."""
        # Test with null byte in path segment which should be caught by security validation
        with pytest.raises(ValueError) as exc_info:
            self.checkpoint._validate_path_segments("round_1", "phase\x00test", "inner_1")
        
        # Check that the error message indicates a security violation
        error_msg = str(exc_info.value)
        assert "Invalid path segment" in error_msg
        assert "control character" in error_msg or "null byte" in error_msg.lower()
    
    def test_write_and_read_checkpoint_metadata_roundtrip(self):
        """Test that checkpoint metadata can be written and read correctly."""
        original_metadata = CheckpointMetadata(
            checkpoint_type="inner",
            outer_round=2,
            phase_label="development",
            inner_index=3,
            basic_score=0.9,
            diffusion_score=0.85,
            critique_count=5,
            actionable_critiques=4,
            synthesis_specificity_score=8,
            timestamp=datetime.now()
        )
        
        # Write metadata
        self.checkpoint.write_checkpoint_metadata(
            original_metadata, 
            "round_2", "phase_development", "inner_4"
        )
        
        # Read it back
        read_metadata = self.checkpoint.read_checkpoint_metadata(
            "round_2", "phase_development", "inner_4"
        )
        
        # Verify all fields match
        assert read_metadata is not None
        assert read_metadata.checkpoint_type == original_metadata.checkpoint_type
        assert read_metadata.outer_round == original_metadata.outer_round
        assert read_metadata.phase_label == original_metadata.phase_label
        assert read_metadata.inner_index == original_metadata.inner_index
        assert read_metadata.basic_score == original_metadata.basic_score
        assert read_metadata.diffusion_score == original_metadata.diffusion_score
        assert read_metadata.critique_count == original_metadata.critique_count
        assert read_metadata.actionable_critiques == original_metadata.actionable_critiques
        assert read_metadata.synthesis_specificity_score == original_metadata.synthesis_specificity_score
        # Timestamp comparison - they should be very close
        time_diff = abs(read_metadata.timestamp - original_metadata.timestamp)
        assert time_diff.total_seconds() < 1  # Within 1 second
    
    def test_read_checkpoint_metadata_returns_none_for_missing_file(self):
        """Test that read_checkpoint_metadata returns None when file doesn't exist."""
        result = self.checkpoint.read_checkpoint_metadata("round_99", "nonexistent")
        assert result is None
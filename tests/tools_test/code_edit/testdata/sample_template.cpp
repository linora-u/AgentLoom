/**
 * @file sample_template.cpp
 * @brief Template class Stack with specialization.
 *
 * Used as a test fixture for code_editor tool testing.
 * Contains deep indentation levels for testing indent-aware matching.
 */

#include <vector>
#include <stdexcept>
#include <string>
#include <type_traits>

namespace containers {

// ---------------------------------------------------------------------------
// Generic Stack
// ---------------------------------------------------------------------------

template <typename T, int MaxSize = 256>
class Stack {
public:
    Stack() : top_(-1) {}

    void push(const T& value) {
        if (is_full()) {
            throw std::overflow_error("Stack overflow");
        }
        data_[++top_] = value;
    }

    T pop() {
        if (is_empty()) {
            throw std::underflow_error("Stack underflow");
        }
        return data_[top_--];
    }

    const T& peek() const {
        if (is_empty()) {
            throw std::underflow_error("Stack is empty");
        }
        return data_[top_];
    }

    bool is_empty() const { return top_ < 0; }
    bool is_full() const { return top_ >= MaxSize - 1; }
    int size() const { return top_ + 1; }

private:
    T data_[MaxSize];
    int top_;
};

// ---------------------------------------------------------------------------
// Template specialization for std::string
// ---------------------------------------------------------------------------

template <>
class Stack<std::string, 256> {
public:
    Stack() {}

    void push(const std::string& value) {
        if (data_.size() >= 256) {
            throw std::overflow_error("Stack overflow");
        }
        data_.push_back(value);
    }

    std::string pop() {
        if (is_empty()) {
            throw std::underflow_error("Stack underflow");
        }
        std::string val = data_.back();
        data_.pop_back();
        return val;
    }

    const std::string& peek() const {
        if (is_empty()) {
            throw std::underflow_error("Stack is empty");
        }
        return data_.back();
    }

    bool is_empty() const { return data_.empty(); }
    bool is_full() const { return data_.size() >= 256; }
    int size() const { return static_cast<int>(data_.size()); }

private:
    std::vector<std::string> data_;
};

// ---------------------------------------------------------------------------
// SFINAE: type trait for checking if a type is stackable
// ---------------------------------------------------------------------------

template <typename T, typename = void>
struct is_stackable : std::false_type {};

template <typename T>
struct is_stackable<T, std::void_t<
    decltype(std::declval<T>() == std::declval<T>()),
    decltype(T(std::declval<T>()))
>> : std::true_type {};

// ---------------------------------------------------------------------------
// Deeply nested function for indentation testing
// ---------------------------------------------------------------------------

template <typename T>
int process_nested_data(const std::vector<std::vector<T>>& matrix) {
    int total = 0;
    for (size_t i = 0; i < matrix.size(); i++) {
        for (size_t j = 0; j < matrix[i].size(); j++) {
            if (matrix[i][j] > 0) {
                if (matrix[i][j] % 2 == 0) {
                    for (int k = 0; k < matrix[i][j]; k++) {
                        if (k > 0) {
                            total += k;
                        }
                    }
                } else {
                    total += matrix[i][j];
                }
            }
        }
    }
    return total;
}

} // namespace containers

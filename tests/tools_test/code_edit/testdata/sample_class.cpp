/**
 * @file sample_class.cpp
 * @brief Calculator class with basic arithmetic operations.
 *
 * Used as a test fixture for code_editor tool testing.
 */

#include <stdexcept>
#include <string>
#include <sstream>

namespace math {

class Calculator {
public:
    Calculator() : result_(0.0), history_count_(0) {}

    explicit Calculator(double initial_value)
        : result_(initial_value), history_count_(0) {}

    ~Calculator() = default;

    // Basic arithmetic
    double add(double a, double b) {
        result_ = a + b;
        history_count_++;
        return result_;
    }

    double subtract(double a, double b) {
        result_ = a - b;
        history_count_++;
        return result_;
    }

    double multiply(double a, double b) {
        result_ = a * b;
        history_count_++;
        return result_;
    }

    double divide(double a, double b) {
        if (b == 0.0) {
            throw std::invalid_argument("Division by zero");
        }
        result_ = a / b;
        history_count_++;
        return result_;
    }

    // Operator overloads
    Calculator operator+(const Calculator& other) const {
        Calculator temp;
        temp.result_ = this->result_ + other.result_;
        return temp;
    }

    Calculator operator-(const Calculator& other) const {
        Calculator temp;
        temp.result_ = this->result_ - other.result_;
        return temp;
    }

    bool operator==(const Calculator& other) const {
        return result_ == other.result_;
    }

    // Accessors
    double get_result() const { return result_; }
    int get_history_count() const { return history_count_; }

    std::string to_string() const {
        std::ostringstream oss;
        oss << "Calculator(result=" << result_
            << ", ops=" << history_count_ << ")";
        return oss.str();
    }

    void reset() {
        result_ = 0.0;
        history_count_ = 0;
    }

private:
    double result_;
    int history_count_;
};

} // namespace math
